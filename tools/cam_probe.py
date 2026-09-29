"""List cameras per backend and measure how long each takes to open and deliver frames.

For every camera index on each backend (MSMF, DSHOW): open latency, time to first frame, negotiated
resolution/FPS/pixel format, measured delivery rate, and whether frames are colour, greyscale (likely
an IR / Windows Hello sensor) or dark (lens covered / privacy shutter closed). Frames are inspected in
memory and discarded; nothing is written to disk.

Usage:
    .venv\\Scripts\\python.exe tools\\cam_probe.py                 # both backends, open params only
    .venv\\Scripts\\python.exe tools\\cam_probe.py --compare-set   # also time no-params and cap.set()
    .venv\\Scripts\\python.exe tools\\cam_probe.py --backends msmf --seconds 5
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

import lookfocus  # noqa: F401  must precede cv2: sets OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS

# isort: split
import cv2

from lookfocus.camera_enum import list_cameras
from lookfocus.capture import BACKENDS, STATIC_MOTION, frame_stats, motion, open_camera, thumbnail


def fourcc_str(v: float) -> str:
    i = int(v)
    s = "".join(chr((i >> (8 * k)) & 0xFF) for k in range(4))
    return s if s.isprintable() and s.strip() else f"0x{i:08x}"


def measure(cap: cv2.VideoCapture, seconds: float) -> dict:
    t0 = t_first = time.perf_counter()
    first = None
    frames = 0
    shape = None
    stats = []
    motions = []
    prev = None
    failures = 0
    while time.perf_counter() - t0 < seconds:
        ok, frame = cap.read()
        if not ok or frame is None:
            failures += 1
            if first is None and time.perf_counter() - t0 > min(seconds, 5.0):
                break
            continue
        if first is None:
            first = time.perf_counter() - t0
            t_first = time.perf_counter()
        frames += 1
        shape = frame.shape
        th = thumbnail(frame)
        if prev is not None:
            motions.append(motion(prev, th))
        prev = th
        if frames % 5 == 1:
            stats.append(frame_stats(frame))
        del frame
    fps = (frames - 1) / (time.perf_counter() - t_first) if first is not None and frames > 1 else 0.0
    motion_med = statistics.median(motions) if motions else None
    return {
        "first_s": first,
        "frames": frames,
        "fps": fps,
        "shape": shape,
        "stats": stats,
        "motion": motion_med,
        "fail": failures,
    }


def classify(stats, motion_med: float | None) -> str:
    if not stats:
        return "no frames"
    grey = sum(s.greyscale for s in stats)
    dark = sum(s.dark for s in stats)
    n = len(stats)
    if dark > n / 2:
        return "DARK (lens covered / very dark room?)"
    if motion_med is not None and motion_med < STATIC_MOTION:
        return "STATIC (placeholder image: software privacy shutter on?)"
    if grey > n / 2:
        return "GREYSCALE (IR sensor? will be skipped)"
    return "colour"


def probe_one(index: int, backend: str, mode: str, args) -> dict:
    row = {"backend": backend, "index": index, "mode": mode}
    w, h = args.width, args.height
    if mode == "params":
        res = open_camera(index, backend, width=w, height=h, timeout_s=args.timeout)
    else:
        res = open_camera(index, backend, width=None, height=None, timeout_s=args.timeout)
    row["open_s"] = res.open_s
    row["error"] = res.error
    if not res.ok:
        return row
    cap = res.cap
    try:
        row["api"] = cap.getBackendName()
        if mode == "set":
            set_times = []
            for prop, val in (
                (cv2.CAP_PROP_FRAME_WIDTH, w),
                (cv2.CAP_PROP_FRAME_HEIGHT, h),
                (cv2.CAP_PROP_FPS, 15),
            ):
                t = time.perf_counter()
                cap.set(prop, val)
                set_times.append(time.perf_counter() - t)
            row["set_s"] = sum(set_times)
            row["set_each"] = set_times
        row["reported"] = (
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            cap.get(cv2.CAP_PROP_FPS),
            fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)),
        )
        row.update(measure(cap, args.seconds))
        row["kind"] = classify(row["stats"], row["motion"])
    finally:
        t = time.perf_counter()
        cap.release()
        row["release_s"] = time.perf_counter() - t
    return row


def fmt_row(r: dict) -> str:
    head = f"  [{r['backend']}:{r['index']}] mode={r['mode']:<6} open={r['open_s']:.2f}s"
    if r.get("error"):
        return f"{head}  FAILED: {r['error']}"
    parts = [head]
    if "set_s" in r:
        parts.append(f"set()x3={r['set_s']:.2f}s ({', '.join(f'{x:.2f}' for x in r['set_each'])})")
    first = f"{r['first_s']:.2f}s" if r.get("first_s") is not None else "never"
    rw, rh, rfps, fcc = r["reported"]
    shape = "x".join(map(str, r["shape"])) if r.get("shape") else "-"
    parts.append(f"first_frame={first} frames={r['frames']} measured_fps={r['fps']:.1f}")
    parts.append(f"reported={rw}x{rh}@{rfps:g} fourcc={fcc} frame_shape={shape}")
    if r["stats"]:
        s = r["stats"][len(r["stats"]) // 2]
        mot = f"{r['motion']:.2f}" if r.get("motion") is not None else "-"
        parts.append(f"luma={s.mean_luma:.0f} chroma={s.chroma:.1f} motion={mot} -> {r['kind']}")
    else:
        parts.append(f"-> {r['kind']}")
    parts.append(f"read_failures={r['fail']} release={r['release_s']:.2f}s")
    return "\n      ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backends", default="msmf,dshow")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--timeout", type=float, default=15.0, help="open timeout per attempt (s)")
    ap.add_argument("--seconds", type=float, default=3.0, help="read duration per camera (s)")
    ap.add_argument(
        "--max-index",
        type=int,
        default=None,
        help="probe indices 0..N even if enumeration finds fewer devices",
    )
    ap.add_argument(
        "--compare-set",
        action="store_true",
        help="also time opening with no params, and opening followed by cap.set() calls",
    )
    args = ap.parse_args()

    from cv2 import videoio_registry as vr

    print(
        f"OpenCV {cv2.__version__}; camera backends compiled in: "
        f"{', '.join(vr.getBackendName(b) for b in vr.getCameraBackends())}"
    )
    backends = [b.strip() for b in args.backends.split(",") if b.strip() in BACKENDS]
    modes = ["params", "none", "set"] if args.compare_set else ["params"]
    rows = []
    for backend in backends:
        devs = list_cameras(backend)
        print(f"\n== {backend.upper()}: {len(devs)} device(s) enumerated ==")
        for d in devs:
            hint = "  [name suggests IR]" if d.looks_ir else ""
            print(f"  index {d.index}: {d.name!r}{hint}\n      path: {d.path or '(none)'}")
        n = len(devs) if args.max_index is None else args.max_index + 1
        for index in range(n):
            for mode in modes:
                r = probe_one(index, backend, mode, args)
                r["name"] = devs[index].name if index < len(devs) else "?"
                rows.append(r)
                print(fmt_row(r), flush=True)

    usable = [r for r in rows if not r.get("error") and r.get("kind") == "colour" and r["mode"] == "params"]
    print("\n== summary ==")
    if not usable:
        print("  No usable colour camera found. Check Settings > Privacy & security > Camera >")
        print("  'Let desktop apps access your camera', that no other app holds the camera exclusively,")
        print("  and that the privacy shutter is open.")
        return 1
    for r in sorted(usable, key=lambda r: r["open_s"] + (r["first_s"] or 0)):
        print(
            f"  {r['backend']}:{r['index']} {r['name']!r}: ready in {r['open_s'] + r['first_s']:.2f}s "
            f"(open {r['open_s']:.2f}s + first frame {r['first_s']:.2f}s), {r['fps']:.1f} fps delivered"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
