"""Live head-pose viewer: angles from each estimator, the raw matrix, FPS and CPU.

Used by tools/debug_pose.py (its own camera) and by the tray's "Debug view" (the running engine's
frames, so no second camera client). Frames are only drawn on screen, never saved.

Shows the camera image (on screen only; frames are never saved or logged) with face landmarks, the
numbers from each pose estimator, and 10-second traces. Runs the real capture pipeline (CaptureThread),
so camera selection, fallback, throttling and the no-face mode behave exactly as in the app.

Press G for the guided check: it asks you to look centre / left / right / up / down and to tilt your head,
then prints a table (median per position, noise, crosstalk) for each estimator. Paste that table back.

Keys:  G guided check   M mirror   V video on/off   R reset traces   Y take camera back   Q / Esc quit

Camera events (another app started using the camera, sharing probe results) are printed to the
terminal with the notification text the app will show.

Usage: .venv\\Scripts\\python.exe tools\\debug_pose.py [--backend msmf|dshow] [--fps 15] [--guided]
"""

from __future__ import annotations

import argparse
import collections
import logging
import math
import os
import statistics
import threading
import time
from collections.abc import Callable

import cv2
import numpy as np

from lookfocus import pose as P
from lookfocus.camera_enum import CameraChoice
from lookfocus.capture import CaptureSettings, CaptureThread, LatestSlot

WIN = "LookFocus debug pose"
VIDEO_W, VIDEO_H = 640, 480
PANEL_W = 470
PLOT_H = 200
FONT = cv2.FONT_HERSHEY_SIMPLEX

# (label, HeadPose field for horizontal, field for vertical, colour BGR, plot scale to "degrees")
ESTIMATORS = [
    ("matrix", "yaw", "pitch", (80, 220, 80), 1.0),
    ("lm3d", "lm_yaw", "lm_pitch", (0, 165, 255), 1.0),
    ("lm2d", "lm2d_yaw", "lm2d_pitch", (255, 160, 60), 120.0),
]
KEY_POINTS = (P.NOSE_TIP, P.NASION, P.FOREHEAD, P.CHIN, P.CHEEK_A, P.CHEEK_B)

GUIDE_STEPS = [
    ("CENTER", "look at the middle of your screen"),
    ("LEFT", "turn your head to look far left"),
    ("RIGHT", "turn your head to look far right"),
    ("UP", "tilt your head back to look up"),
    ("DOWN", "tilt your head forward to look down"),
    ("TILT L", "keep facing the screen, tilt head toward LEFT shoulder"),
    ("TILT R", "keep facing the screen, tilt head toward RIGHT shoulder"),
    ("CENTER2", "look at the middle of your screen again"),
]
SETTLE_S, COLLECT_S = 1.5, 2.0
FIELDS = ["yaw", "pitch", "roll", "lm_yaw", "lm_pitch", "lm2d_yaw", "lm2d_pitch"]


class CpuMeter:
    """Whole-process CPU (all threads, including mediapipe's native ones) via time.process_time()."""

    def __init__(self) -> None:
        self.c0, self.w0 = time.process_time(), time.perf_counter()
        self.one_core = 0.0

    def update(self) -> None:
        w = time.perf_counter()
        if w - self.w0 >= 1.0:
            c = time.process_time()
            self.one_core = (c - self.c0) / (w - self.w0) * 100
            self.c0, self.w0 = c, w

    def text(self) -> str:
        n = os.cpu_count() or 1
        return f"CPU {self.one_core:5.1f}% of one core = {self.one_core / n:4.1f}% of {n} CPUs"


class Guided:
    """Timed prompt sequence; collects pose samples during each step's collection window."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.data: dict[str, list[P.HeadPose]] = {name: [] for name, _ in GUIDE_STEPS}
        self.missed: dict[str, int] = {name: 0 for name, _ in GUIDE_STEPS}

    def state(self, now: float) -> tuple[int, str, float, bool] | None:
        """(step index, phase text, fraction of the step elapsed, collecting?) or None when finished."""
        el = now - self.t0
        step_len = SETTLE_S + COLLECT_S
        i = int(el // step_len)
        if i >= len(GUIDE_STEPS):
            return None
        within = el - i * step_len
        return i, GUIDE_STEPS[i][1], within / step_len, within >= SETTLE_S

    def feed(self, now: float, sample: P.PoseSample) -> None:
        st = self.state(now)
        if st is None or not st[3]:
            return
        name = GUIDE_STEPS[st[0]][0]
        if sample.pose is not None:
            self.data[name].append(sample.pose)
        else:
            self.missed[name] += 1

    def report(self) -> str:
        med: dict[str, dict[str, float]] = {}
        sd: dict[str, float] = {}
        for name, _ in GUIDE_STEPS:
            poses = self.data[name]
            med[name] = {
                f: statistics.median(getattr(p, f) for p in poses) if poses else math.nan for f in FIELDS
            }
        center = self.data["CENTER"]
        for f in FIELDS:
            vals = [getattr(p, f) for p in center]
            sd[f] = statistics.pstdev(vals) if len(vals) > 1 else math.nan

        steps = [name for name, _ in GUIDE_STEPS]
        lines = ["", "=== LookFocus guided pose check ===", ""]
        lines.append("samples per step: " + ", ".join(f"{n}={len(self.data[n])}" for n in steps))
        if any(self.missed.values()):
            lines.append("no-face frames:   " + ", ".join(f"{n}={self.missed[n]}" for n in steps))
        lines.append("")
        head = f"{'field':<11}" + "".join(f"{n:>9}" for n in steps) + f"{'sd@ctr':>9}"
        lines.append(head)
        for f in FIELDS:
            row = f"{f:<11}" + "".join(f"{med[n][f]:9.2f}" for n in steps) + f"{sd[f]:9.3f}"
            lines.append(row)

        def m(step: str, f: str) -> float:
            return med[step][f]

        lines.append("")
        lines.append(
            "quality (span = LEFT-RIGHT or UP-DOWN difference; bigger SNR better, smaller others better):"
        )
        lines.append(f"{'estimator axis':<17}{'span':>8}{'SNR':>8}{'xtalk':>8}{'tilt':>8}{'repeat':>8}")
        for label, fh, fv, _, _ in ESTIMATORS:
            for axis, f, other_pair in (("horiz", fh, ("UP", "DOWN")), ("vert", fv, ("LEFT", "RIGHT"))):
                a, b = ("LEFT", "RIGHT") if axis == "horiz" else ("UP", "DOWN")
                span = abs(m(a, f) - m(b, f))
                snr = span / sd[f] if sd[f] and sd[f] > 0 else math.inf
                # crosstalk: movement on this axis while turning along the other axis
                xt = abs(m(other_pair[0], f) - m(other_pair[1], f)) / span if span else math.nan
                tilt = abs(m("TILT L", f) - m("TILT R", f)) / span if span else math.nan
                rep = abs(m("CENTER", f) - m("CENTER2", f)) / span if span else math.nan
                lines.append(f"{label + ' ' + axis:<17}{span:8.2f}{snr:8.1f}{xt:8.2f}{tilt:8.2f}{rep:8.2f}")
        lines.append("")
        lines.append("xtalk/tilt/repeat are fractions of span: 0.10 means 10% of the full left-right swing.")
        return "\n".join(lines)


def put(img, text, org, scale=0.5, color=(230, 230, 230), thick=1):
    # Drop shadow at the same thickness: a thicker Hershey stroke also widens the glyph advance, so a
    # thick black "outline" would run past the text (the ghosting seen in the M1 screenshot).
    cv2.putText(img, text, (org[0] + 1, org[1] + 1), FONT, scale, (0, 0, 0), thick, cv2.LINE_AA)
    cv2.putText(img, text, org, FONT, scale, color, thick, cv2.LINE_AA)


def draw_video(canvas, frame, pts, mirror: bool, show_video: bool) -> None:
    view = canvas[:VIDEO_H, :VIDEO_W]
    if frame is not None and show_video:
        f = cv2.resize(frame, (VIDEO_W, VIDEO_H)) if frame.shape[:2] != (VIDEO_H, VIDEO_W) else frame
        view[:] = cv2.flip(f, 1) if mirror else f
    else:
        view[:] = 20
    if pts is None:
        return
    xs = pts[:, 0] * VIDEO_W
    ys = pts[:, 1] * VIDEO_H
    if mirror:
        xs = VIDEO_W - xs
    for x, y in zip(xs[::3], ys[::3], strict=False):
        cv2.circle(view, (int(x), int(y)), 1, (200, 200, 200), -1)
    for i in KEY_POINTS:
        cv2.circle(view, (int(xs[i]), int(ys[i])), 4, (0, 255, 255), -1)


def draw_panel(
    canvas, sample: P.PoseSample | None, cpu: CpuMeter, mirror: bool, show_video: bool, extra: tuple = ()
) -> None:
    x0 = VIDEO_W + 12
    panel = canvas[:, VIDEO_W:]
    panel[:] = 32
    y = 24

    def line(text, scale=0.5, color=(230, 230, 230), dy=20):
        nonlocal y
        put(canvas, text, (x0, y), scale, color)
        y += dy

    if sample is None:
        line("starting capture...")
        return
    colors = {
        "yielded": (255, 120, 200),
        "tracking": (80, 220, 80),
        "lost": (0, 200, 255),
        "no_face": (0, 140, 255),
        "error": (60, 60, 255),
    }
    line(f"status: {sample.status}", 0.6, colors.get(sample.status, (230, 230, 230)), 22)
    line(f"camera: {sample.camera or '-'}")
    line(f"processed {sample.fps:4.1f} fps   inference {sample.infer_ms:5.1f} ms")
    line(cpu.text())
    for text in extra:
        line(text, 0.45, (200, 200, 120), 18)
    if sample.message:
        for chunk in [sample.message[i : i + 60] for i in range(0, min(len(sample.message), 240), 60)]:
            line(chunk, 0.45, (0, 200, 255), 18)
    y += 6
    hp = sample.pose
    if hp is None:
        line("no face", 0.7, (0, 140, 255), 30)
    else:
        line("estimator      horiz     vert     roll", 0.5, (180, 180, 180))
        for label, fh, fv, color, _ in ESTIMATORS:
            roll = f"{hp.roll:8.1f}" if label == "matrix" else ""
            line(f"{label:<10}{getattr(hp, fh):9.2f}{getattr(hp, fv):9.2f}{roll}", 0.6, color, 24)
        line("(matrix/lm3d in degrees; lm2d unitless ratio)", 0.4, (150, 150, 150), 22)
        line(f"translation  x {hp.tx:7.2f}  y {hp.ty:7.2f}  z {hp.tz:7.2f}")
        line(f"matrix layout detected: {hp.layout}", 0.5, (230, 230, 230), 22)
        if hp.matrix:
            line("raw matrix (as returned, row by row):", 0.45, (180, 180, 180), 18)
            for r in range(4):
                vals = hp.matrix[r * 4 : r * 4 + 4]
                line("  " + " ".join(f"{v:8.3f}" for v in vals), 0.45, (210, 210, 210), 17)
    y = VIDEO_H - 10
    put(
        canvas,
        f"G guided  M mirror({'on' if mirror else 'off'})  V video({'on' if show_video else 'off'})  "
        "R reset  Y take cam  Q quit",
        (x0, y),
        0.42,
        (170, 170, 170),
    )


def draw_traces(canvas, trace: collections.deque, now: float, window_s: float = 10.0) -> None:
    area = canvas[VIDEO_H:, :]
    area[:] = 24
    w = canvas.shape[1]
    half = w // 2
    for k, (title, idx) in enumerate((("horizontal (yaw)", 1), ("vertical (pitch)", 2))):
        x0, x1 = k * half + 40, (k + 1) * half - 10
        top, bot = VIDEO_H + 25, VIDEO_H + PLOT_H - 15
        mid = (top + bot) // 2
        cv2.rectangle(canvas, (x0, top), (x1, bot), (70, 70, 70), 1)
        cv2.line(canvas, (x0, mid), (x1, mid), (60, 60, 60), 1)
        put(canvas, title + "   +/-60 deg full scale", (x0, top - 8), 0.45, (190, 190, 190))
        put(canvas, "+60", (x0 - 36, top + 10), 0.35, (150, 150, 150))
        put(canvas, "-60", (x0 - 36, bot), 0.35, (150, 150, 150))
        for _label, fh, fv, color, scale in ESTIMATORS:
            field = fh if idx == 1 else fv
            ptsl = []
            for t, hp in trace:
                if hp is None:
                    continue
                v = getattr(hp, field) * scale
                if not math.isfinite(v):
                    continue
                px = x1 - (now - t) / window_s * (x1 - x0)
                py = mid - max(-60.0, min(60.0, v)) / 60.0 * (mid - top)
                ptsl.append((int(px), int(py)))
            if len(ptsl) > 1:
                cv2.polylines(canvas, [np.array(ptsl, dtype=np.int32)], False, color, 1, cv2.LINE_AA)
        if k == 0:
            lx = x0 + 8
            for label, _, _, color, scale in ESTIMATORS:
                put(canvas, label + (f" x{scale:g}" if scale != 1 else ""), (lx, bot - 8), 0.4, color)
                lx += 110


def draw_guide(canvas, guided: Guided, now: float) -> None:
    st = guided.state(now)
    if st is None:
        return
    i, text, frac, collecting = st
    name = GUIDE_STEPS[i][0]
    color = (80, 220, 80) if collecting else (0, 200, 255)
    overlay = canvas[:VIDEO_H, :VIDEO_W]
    cv2.rectangle(overlay, (0, 0), (VIDEO_W, 90), (0, 0, 0), -1)
    put(canvas, f"{i + 1}/{len(GUIDE_STEPS)}  {name}", (16, 36), 1.0, color, 2)
    put(
        canvas,
        text + ("   (hold still, measuring)" if collecting else "   (get ready)"),
        (16, 68),
        0.5,
        color,
    )
    cv2.rectangle(canvas, (0, 84), (int(VIDEO_W * frac), 90), color, -1)


def run_viewer(
    frames: LatestSlot,
    poses: LatestSlot,
    *,
    extra_lines: Callable[[], list[str]] | None = None,
    on_key: Callable[[int], None] | None = None,
    stop: threading.Event | None = None,
    seconds: float = 0,
    mirror: bool = True,
    show_video: bool = True,
    guided_on_face: bool = False,
    report: Callable[[str], None] = print,
) -> None:
    """Window loop. `frames` holds (frame, landmarks, sample) from a CaptureThread debug hook; `poses` the
    capture's PoseSample slot (for status while no frames flow). Returns when the window is closed, Q/Esc
    is pressed, `stop` is set or `seconds` elapse. Must run on one thread (HighGUI)."""
    canvas = np.zeros((VIDEO_H + PLOT_H, VIDEO_W + PANEL_W, 3), np.uint8)
    cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)
    cpu = CpuMeter()
    trace: collections.deque = collections.deque()
    guided: Guided | None = None
    want_guided = guided_on_face
    seen = 0
    frame = pts = None
    t_quit = time.monotonic() + seconds if seconds else math.inf
    try:
        while stop is None or not stop.is_set():
            seen, item = frames.wait_newer(seen, 0.1)
            now = time.monotonic()
            _, status_sample = poses.get()
            sample = status_sample
            if item is not None:
                frame, pts, fsample = item
                if status_sample is None or fsample.t >= status_sample.t:
                    sample = fsample
                if not trace or trace[-1][0] != fsample.t:
                    trace.append((fsample.t, fsample.pose))
                    if guided is not None:
                        guided.feed(now, fsample)
                if want_guided and fsample.pose is not None and guided is None:
                    guided, want_guided = Guided(), False
            if sample is not None and sample.status in ("error", "opening", "paused", "yielded", "stopped"):
                frame = pts = None
            while trace and now - trace[0][0] > 10.0:
                trace.popleft()
            cpu.update()

            draw_video(canvas, frame, pts, mirror, show_video)
            draw_panel(canvas, sample, cpu, mirror, show_video, tuple(extra_lines() if extra_lines else ()))
            draw_traces(canvas, trace, now)
            if guided is not None:
                if guided.state(now) is None:
                    report(guided.report())
                    guided = None
                else:
                    draw_guide(canvas, guided, now)
            cv2.imshow(WIN, canvas)

            key = cv2.waitKey(1) & 0xFF
            closed = cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1
            if key in (ord("q"), 27) or closed or now > t_quit:
                break
            if key == ord("m"):
                mirror = not mirror
            elif key == ord("v"):
                show_video = not show_video
            elif key == ord("r"):
                trace.clear()
            elif key == ord("g"):
                guided = Guided()
            elif key != 255 and on_key is not None:
                on_key(key)
    finally:
        cv2.destroyWindow(WIN)
        cv2.waitKey(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=["msmf", "dshow"], default="msmf")
    ap.add_argument("--no-fallback", action="store_true", help="don't try the other backend")
    ap.add_argument("--camera-index", type=int, default=None)
    ap.add_argument("--camera-name", default="")
    ap.add_argument("--fps", type=float, default=15.0)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--no-video", action="store_true", help="draw landmarks on black instead of the image")
    ap.add_argument("--no-mirror", action="store_true")
    ap.add_argument("--guided", action="store_true", help="start the guided check once a face is seen")
    ap.add_argument("--seconds", type=float, default=0, help=argparse.SUPPRESS)  # auto-quit (smoke tests)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = CaptureSettings(
        backend=args.backend,
        fallback=not args.no_fallback,
        camera=CameraChoice(name=args.camera_name, index=args.camera_index),
        width=args.width,
        height=args.height,
        fps=args.fps,
    )
    poses: LatestSlot[P.PoseSample] = LatestSlot()
    frames: LatestSlot = LatestSlot()
    last_event = [""]

    def on_event(kind: str, info: dict) -> None:
        if kind == "sharing":
            print(f"[camera sharing] {info['state']} (probe; MEPSGFlag hint={info.get('hint')})", flush=True)
        elif kind in ("yielded", "busy"):
            print(f"[{kind}] NOTIFICATION: {info['title']}", flush=True)
            print(f"    {info['body']}", flush=True)
            last_event[0] = f"{kind}: {', '.join(info.get('apps', []))}"
        elif kind == "resumed":
            print(f"[resumed] manual={info.get('manual')} apps={info.get('apps', [])}", flush=True)
            last_event[0] = "resumed" + (" (manual)" if info.get("manual") else "")

    capture = CaptureThread(
        settings, poses, debug_hook=lambda f, p, s: frames.put((f, p, s)), on_event=on_event
    )
    capture.start()
    reports: list[str] = []

    def lines() -> list[str]:
        out = [f"camera sharing: {capture.sharing.value}"]
        if last_event[0]:
            out.append(f"last camera event: {last_event[0]}")
        return out

    def on_key(key: int) -> None:
        if key == ord("y"):
            capture.resume_from_yield()

    def report(text: str) -> None:
        print(text, flush=True)
        reports.append(text)

    try:
        run_viewer(
            frames,
            poses,
            extra_lines=lines,
            on_key=on_key,
            seconds=args.seconds,
            mirror=not args.no_mirror,
            show_video=not args.no_video,
            guided_on_face=args.guided,
            report=report,
        )
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()
    if reports:
        print("\n(guided report(s) above; paste them back)")
    return 0
