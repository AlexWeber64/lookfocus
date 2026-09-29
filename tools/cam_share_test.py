"""Can another app use the camera while LookFocus holds it?

Holds camera `--index` open on `--hold` backend and reads continuously, then launches a second
process that opens the same camera on each `--child` backend and reads for a few seconds (the stand-in
for a video-call app). Reports whether the second open succeeded, whether it got real frames, and
whether the holder kept receiving frames meanwhile. Frames stay in memory; nothing is written.

Usage:  .venv\\Scripts\\python.exe tools\\cam_share_test.py [--index 0] [--hold msmf] [--child msmf,dshow]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time

import lookfocus  # noqa: F401  must precede cv2: sets OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS

# isort: split
from lookfocus.capture import frame_stats, open_camera


def child(index: int, backend: str, seconds: float) -> None:
    res = open_camera(index, backend, timeout_s=10)
    out = {"backend": backend, "opened": res.ok, "open_s": round(res.open_s, 2), "error": res.error}
    if res.ok:
        frames = fails = 0
        chroma = []
        t_end = time.perf_counter() + seconds
        while time.perf_counter() < t_end:
            ok, frame = res.cap.read()
            if ok and frame is not None:
                frames += 1
                if frames % 10 == 1:
                    chroma.append(round(frame_stats(frame).chroma, 1))
            else:
                fails += 1
                time.sleep(0.05)
        res.cap.release()
        out.update(frames=frames, fails=fails, chroma=chroma)
    print(json.dumps(out))


class Holder(threading.Thread):
    def __init__(self, cap) -> None:
        super().__init__(daemon=True)
        self.cap = cap
        self.log: list[tuple[float, bool]] = []
        self.halt = threading.Event()

    def run(self) -> None:
        while not self.halt.is_set():
            ok, frame = self.cap.read()
            self.log.append((time.perf_counter(), bool(ok and frame is not None)))
            if not ok:
                time.sleep(0.05)

    def window(self, t0: float, t1: float) -> tuple[int, int]:
        seg = [ok for t, ok in self.log if t0 <= t <= t1]
        return sum(seg), len(seg) - sum(seg)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--hold", default="msmf")
    ap.add_argument("--child", default="msmf,dshow")
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--_child", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args._child:
        child(args.index, args._child, args.seconds)
        return 0

    res = open_camera(args.index, args.hold, timeout_s=10)
    if not res.ok:
        print(f"holder could not open {args.hold}:{args.index}: {res.error}")
        return 1
    holder = Holder(res.cap)
    holder.start()
    time.sleep(2.0)
    print(f"holder: {args.hold}:{args.index} open, reading")
    for backend in [b.strip() for b in args.child.split(",") if b.strip()]:
        t0 = time.perf_counter()
        proc = subprocess.run(
            [
                sys.executable,
                __file__,
                "--index",
                str(args.index),
                "--seconds",
                str(args.seconds),
                "--_child",
                backend,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        t1 = time.perf_counter()
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("{")), "")
        info = json.loads(line) if line else {"error": f"child crashed rc={proc.returncode}"}
        ok, bad = holder.window(t0, t1)
        print(f"  second app via {backend}: {info}")
        print(f"  holder meanwhile: {ok} frames ok, {bad} failed reads")
        time.sleep(1.0)
    holder.halt.set()
    holder.join()
    res.cap.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
