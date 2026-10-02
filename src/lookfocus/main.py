"""LookFocus command line.

    lookfocus calibrate [--zones N]      optional camera-sharing setup step, then dots on each display
    lookfocus watch [--zones N]          print "facing: <display>" live
    lookfocus camera-sharing             check / set up Windows' multi-app camera setting

(The tray app arrives in M5.) Run from the venv: `.venv\\Scripts\\lookfocus.exe ...` or `python -m lookfocus`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from lookfocus import calibration as C
from lookfocus import paths
from lookfocus.displays import LayoutError, LogicalDisplay, build_displays, require_multiple

log = logging.getLogger("lookfocus")
POSE_SOURCES = ("matrix", "lm3d", "lm2d")


def _displays(zones: int) -> list[LogicalDisplay]:
    from lookfocus import monitors

    mode = monitors.enable_dpi_awareness()
    mons = monitors.list_monitors()
    log.info("DPI awareness: %s; %d monitor(s)", mode, len(mons))
    ds = build_displays(mons, zones)
    require_multiple(ds, zones)
    return ds


def _capture_settings(args, **kw):
    from lookfocus.capture import CaptureSettings

    return CaptureSettings(backend=args.backend, fallback=not args.no_fallback, **kw)


def _print_layout(ds: list[LogicalDisplay]) -> None:
    for i, d in enumerate(ds, 1):
        r = d.rect
        print(f"  {i}. {d.label}: {r.width}x{r.height} at ({r.left},{r.top})")


def cmd_calibrate(args) -> int:
    from lookfocus import camera_sharing
    from lookfocus import overlay as O
    from lookfocus.calibrator import Calibrator, CalibratorSettings, Cancelled, separability
    from lookfocus.capture import CaptureThread, LatestSlot

    ds = _displays(args.zones)
    print(f"LookFocus calibration: {len(ds)} displays")
    _print_layout(ds)
    print()
    if not args.skip_camera_sharing:
        camera_sharing.run_setup_step(_capture_settings(args))
        print()
    print("Next: a dark screen appears on each display in turn with 5 dots.")
    print("Look at each dot the way you would when working there (turn your head naturally).")
    print("Ctrl+C here cancels (Esc also works if the calibration screen has focus).")
    try:
        input("Press Enter to start... ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1

    slot = LatestSlot()
    capture = CaptureThread(_capture_settings(args, probe_sharing=False), slot)
    capture.start()
    ov = O.Overlay()
    try:
        cal = Calibrator(slot, ov, CalibratorSettings(source=args.pose_source)).run(ds, args.zones)
    except (Cancelled, KeyboardInterrupt):
        print("\nCalibration cancelled; nothing saved.")
        return 1
    except (TimeoutError, RuntimeError) as e:
        print(f"\nCalibration failed: {e}")
        return 1
    finally:
        ov.close()
        capture.stop()

    path = Path(args.calibration)
    C.save(cal, path)
    print(f"\nSaved {path}")
    _sync_config_zones(args.zones)
    src = args.pose_source
    print(f"{'display':<12}{'center h':>10}{'center v':>10}{'width':>9}{'height':>9}{'noise h/v':>13}")
    for d in cal.displays:
        by = {p.name: p for p in d.points}
        c, left, right, top, bot = (by[n].pose[src] for n in ("center", "left", "right", "top", "bottom"))
        sd = by["center"].spread[src]
        short = d.label.split(" (")[0]
        print(
            f"{short:<12}{c[0]:10.1f}{c[1]:10.1f}{abs(right[0] - left[0]):9.1f}{abs(bot[1] - top[1]):9.1f}"
            f"{sd[0]:8.2f}/{sd[1]:.2f}"
        )
    problems, report = separability(cal, src)
    print()
    for line in report:
        print(f"  {line}")
    print()
    if problems:
        print("Warning: some displays are hard to tell apart by head pose:")
        for p in problems:
            print(f"  - {p}")
        print("Recalibrate turning your head (not just your eyes) toward each display, or use fewer zones.")
    else:
        print("All displays are distinguishable. Try `lookfocus watch` next.")
    return 0


def _sync_config_zones(zones: int) -> None:
    """Keep config.json's "zones" in line with the calibration just made (the tray app reads the config)."""
    from dataclasses import replace

    from lookfocus import config as K

    cfg_path = paths.data_dir() / "config.json"
    cfg, _ = K.load(cfg_path)
    if cfg.zones != zones or not cfg_path.exists():
        K.save(replace(cfg, zones=zones), cfg_path)
        print(f'Set "zones": {zones} in {cfg_path}')


def cmd_watch(args) -> int:
    from lookfocus.capture import CaptureThread, LatestSlot
    from lookfocus.classifier import Classifier

    cal = C.load(Path(args.calibration))
    if cal is None:
        print(f"No calibration at {args.calibration}. Run `lookfocus calibrate` first.")
        return 1
    zones = cal.zones if args.zones is None else args.zones
    ds = _displays(zones)
    missing, added = cal.check_layout(ds)
    if missing or added:
        print("The display layout changed since calibration:")
        for m in missing:
            print(f"  - calibrated display not present now: {m}")
        for a in added:
            print(f"  - new, uncalibrated display: {a}")
        print(f"Run `lookfocus calibrate{f' --zones {zones}' if zones else ''}` to recalibrate.")
        return 1
    clf = Classifier.from_calibration(cal, args.pose_source, none_margin=args.none_margin)
    labels = {d.id: d.label for d in ds}
    print(f"Watching {len(ds)} displays (pose source {args.pose_source}). Ctrl+C to stop.")

    def on_event(kind: str, info: dict) -> None:
        if kind in ("yielded", "busy"):
            print(f"[{kind}] {info['title']}: {info['body']}", flush=True)
        elif kind == "resumed":
            print("[resumed] camera back", flush=True)

    slot = LatestSlot()
    capture = CaptureThread(_capture_settings(args), slot, on_event=on_event)
    capture.start()
    last = None
    frame_seen = (0, 0)
    seen = 0
    try:
        while capture.is_alive():
            seen, s = slot.wait_newer(seen, 1.0)
            if s is None:
                continue
            if s.frame != frame_seen:
                frame_seen = s.frame
                if cal.camera_mismatch(s.frame):
                    print(
                        f"Note: the camera now delivers {s.frame[0]}x{s.frame[1]} but calibration used "
                        f"{cal.camera[0]}x{cal.camera[1]} (camera-sharing setting changed?). If results look "
                        "off, recalibrate.",
                        flush=True,
                    )
            if s.pose is None:
                state, detail = f"- ({s.status})", s.message
            else:
                r = clf.classify(*s.pose.features(args.pose_source))
                if r.display is None:
                    state, detail = "none", f"outside every display by {min(r.outside.values()):.1f}"
                else:
                    state = labels.get(r.display, r.display)
                    fx, fy = r.position if r.position else (float("nan"),) * 2
                    detail = f"at ({fx:.2f}, {fy:.2f}), distance {r.distance[r.display]:.1f}"
            if state != last or args.verbose:
                stamp = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(f"{stamp} facing: {state}" + (f"   [{detail}]" if detail else ""), flush=True)
                last = state
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()
    return 0


def cmd_camera_sharing(args) -> int:
    from lookfocus import camera_sharing

    camera_sharing.run_setup_step(_capture_settings(args))
    return 0


def cmd_run(args) -> int:
    """The full engine in a console (the tray app wraps the same Daemon in M5)."""
    import time
    from dataclasses import replace

    from lookfocus import config as K
    from lookfocus import windows_focus as wf
    from lookfocus.daemon import Daemon, settings_from_config

    if args.elevated and not wf.we_are_elevated():
        argv = [a for a in sys.argv[1:] if a != "--elevated"]
        if wf.relaunch_elevated(["-m", "lookfocus", *argv]):
            print("Relaunched as administrator in a new window.")
            return 0
        print("Elevation was cancelled or failed.")
        return 1
    cal = C.load(Path(args.calibration))
    if cal is None:
        print(f"No calibration at {args.calibration}. Run `lookfocus calibrate` first.")
        return 1
    zones = cal.zones if args.zones is None else args.zones
    ds = _displays(zones)
    missing, added = cal.check_layout(ds)
    if missing or added:
        print("The display layout changed since calibration; run `lookfocus calibrate` again.")
        return 1
    cfg, problems = K.load(paths.data_dir() / "config.json")  # flags given on the command line win
    for problem in problems:
        print(f"config: {problem}")
    flags = {
        "fps": args.fps,
        "dwell_ms": args.dwell_ms,
        "typing_dwell_ms": args.typing_dwell_ms,
        "typing_grace_ms": args.typing_grace_ms,
        "mouse_grace_ms": args.mouse_grace_ms,
        "hysteresis": args.hysteresis,
        "none_margin": args.none_margin,
        "pose_source": args.pose_source,
        "camera_backend": args.backend,
        "zones": zones,
    }
    cfg = replace(cfg, **{k: v for k, v in flags.items() if v is not None})
    if args.move_cursor:
        cfg = replace(cfg, move_cursor_on_switch=True)
    if args.no_alt:
        cfg = replace(cfg, allow_alt_fallback=False)
    settings = settings_from_config(cfg, dry_run=args.dry_run)

    def stamp() -> str:
        return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def on_status(status: str, detail: str) -> None:
        print(f"{stamp()} [{status}] {detail}", flush=True)

    def on_event(kind: str, info: dict) -> None:
        if kind == "switched":
            print(
                f"{stamp()} -> {info['display']}: {info['window']} ({info['stage']}, {info['ms']:.0f} ms)",
                flush=True,
            )
        elif kind in ("camera_yielded", "camera_busy"):
            print(f"{stamp()} NOTIFICATION {info['title']}: {info['body']}", flush=True)
        else:
            print(f"{stamp()} {kind}: {info}", flush=True)

    d = Daemon(settings, cal, on_status=on_status, on_event=on_event)
    mode = " (dry run: no focus changes)" if args.dry_run else ""
    elev = "elevated" if wf.we_are_elevated() else "not elevated"
    print(f"LookFocus running on {len(ds)} displays{mode}, {elev}. Ctrl+C to stop.")
    d.start()
    try:
        if args.stats:
            _print_stats(d, stamp)
        else:
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        d.stop()
    return 0


def _print_stats(d, stamp, every: float = 10.0) -> None:
    """Every `every` s: whole-process CPU (all threads incl. mediapipe's), processed fps, inference time."""
    import os
    import statistics
    import time

    seen, n, faces, infer = 0, 0, 0, []
    c0, w0 = time.process_time(), time.perf_counter()
    while True:
        seen, s = d.poses.wait_newer(seen, 0.5)
        if s is not None and s.status in ("tracking", "lost", "no_face"):
            n += 1
            if s.pose is not None:
                faces += 1
                infer.append(s.infer_ms)
        wall = time.perf_counter() - w0
        if wall >= every:
            cpu = (time.process_time() - c0) / wall * 100
            ncpu = os.cpu_count() or 1
            inf = f", inference {statistics.median(infer):.1f} ms" if infer else ""
            print(
                f"{stamp()} stats: CPU {cpu / ncpu:.1f}% of the machine ({cpu:.0f}% of one core), "
                f"{n / wall:.1f} frames/s processed, face in {100 * faces / max(n, 1):.0f}%{inf}",
                flush=True,
            )
            n, faces, infer = 0, 0, []
            c0, w0 = time.process_time(), time.perf_counter()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lookfocus", description="Move keyboard focus to the display you face.")
    ap.add_argument("-v", "--verbose-log", action="store_true", help="debug logging")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, zones_default, from_config=False):
        p.add_argument(
            "--zones",
            type=int,
            default=zones_default,
            help="split the primary monitor into N side-by-side logical displays",
        )
        p.add_argument("--backend", choices=["msmf", "dshow"], default=None if from_config else "msmf")
        p.add_argument("--no-fallback", action="store_true", help="don't try the other camera backend")
        p.add_argument("--pose-source", choices=POSE_SOURCES, default=None if from_config else "matrix")
        p.add_argument("--calibration", default=str(paths.data_dir() / "calibration.json"))

    p = sub.add_parser("calibrate", help="calibrate head pose for each display")
    common(p, 0)
    p.add_argument("--skip-camera-sharing", action="store_true", help="skip the optional camera-sharing step")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("watch", help='print "facing: <display>" live')
    common(p, None)
    p.add_argument("--none-margin", type=float, default=6.0, help="how far outside all displays is none")
    p.add_argument("--verbose", action="store_true", help="print every sample, not just changes")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("camera-sharing", help="check Windows' multi-app camera setting")
    common(p, 0)
    p.set_defaults(func=cmd_camera_sharing)

    p = sub.add_parser("run", help="run LookFocus in this console (switches focus)")
    common(p, None, from_config=True)
    p.add_argument("--dry-run", action="store_true", help="decide and report, but never move focus")
    p.add_argument("--stats", action="store_true", help="print CPU / fps / inference time every 10 s")
    p.add_argument("--fps", type=float, default=None, help="default: config (15)")
    p.add_argument("--dwell-ms", type=int, default=None, help="default: config (300)")
    p.add_argument("--typing-dwell-ms", type=int, default=None, help="default: config (1000)")
    p.add_argument("--typing-grace-ms", type=int, default=None, help="default: config (2000)")
    p.add_argument("--mouse-grace-ms", type=int, default=None, help="default: config (1500)")
    p.add_argument("--hysteresis", type=float, default=None, help="extra turn needed to leave a display")
    p.add_argument("--none-margin", type=float, default=None, help="how far outside all displays is none")
    p.add_argument("--move-cursor", action="store_true", help="also move the pointer to the new display")
    p.add_argument("--no-alt", action="store_true", help="never use the Alt-tap activation fallback")
    p.add_argument("--elevated", action="store_true", help="relaunch as administrator (UAC prompt)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("tray", help="the tray app (also what plain `lookfocus` and autostart run)")
    p.add_argument("--elevated", action="store_true", help="run as administrator (UAC prompt)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in (None, "tray"):  # no command = the tray app (what autostart runs)
        from lookfocus import tray

        return tray.main(elevated=getattr(args, "elevated", False))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose_log else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except LayoutError as e:
        print(f"LookFocus: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
