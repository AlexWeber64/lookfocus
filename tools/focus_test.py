"""Cycle keyboard focus between one window per logical display and report which activation stage worked.

LookFocus runs in the background, where Windows' foreground lock blocks a plain SetForegroundWindow. This
tool runs the same escalation ladder as the app (windows_focus.focus_window -> targeting.activate),
verifies every switch with GetForegroundWindow, checks that keyboard input really goes to the target, and
reports per-stage success rates.

By default the switching runs in a console-less child process (pythonw), like the real app: a program
attached to a console may take focus while its console is in front, which made an in-terminal run look
better than reality (D-029). --console runs it in this terminal instead.

Windows per display:
  default        the topmost window already on each display / zone
  --windows Q..  one query per display, in display order. Best match wins: exact program name, then exact
                 window class, then program name / title / class containing the query. The terminal running
                 this test is matched last. Chosen windows are moved into their display unless --no-place.
  --pick         choose from a numbered list (moved into place unless --no-place)

Zone mode on one monitor:  --zones 2 --windows WindowsTerminal firefox
Elevated windows: open an administrator PowerShell and include it (--windows Administrator explorer) from a
normal, non-elevated terminal: expect a clear UIPI failure. --elevated runs the test as administrator.

Don't type during the test. Move the mouse (or click) to abort.
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

import lookfocus  # noqa: F401

# isort: split
from lookfocus import camera_sharing, monitors, paths
from lookfocus import windows_focus as wf
from lookfocus.displays import LayoutError, LogicalDisplay, Rect, build_displays, require_multiple
from lookfocus.targeting import STAGES, WindowInfo, choose_target, display_of

ABORT_PX = 40
_log = None  # child mode: lines go to this file instead of stdout (pythonw has no console)


def out(line: str = "") -> None:
    if _log is not None:
        _log.write(line + "\n")
        _log.flush()
    else:
        print(line, flush=True)


def work_area(d: LogicalDisplay, mons) -> Rect:
    m = next((m for m in mons if m.key == d.monitor_key), None)
    if m is None:
        return d.rect
    w, r = m.work, d.rect
    return Rect(max(w.left, r.left), max(w.top, r.top), min(w.right, r.right), min(w.bottom, r.bottom))


def match_rank(w: WindowInfo, q: str) -> int | None:
    """Lower is better; None = no match. The test's own terminal (its title holds the command line, so it
    contains every query) only matches if nothing else does."""
    q = q.casefold()
    exe, cls, title = w.exe.casefold(), w.cls.casefold(), w.title.casefold()
    for rank, hit in enumerate((exe == q, cls == q, q in exe, q in title, q in cls)):
        if hit:
            return rank + (10 if "focus_test" in title else 0)
    return None


def choose_windows(args, ds, mons) -> list[WindowInfo] | None:
    cands = wf.candidate_windows()
    if args.windows:
        if len(args.windows) != len(ds):
            print(f"--windows needs {len(ds)} queries (one per display), got {len(args.windows)}")
            return None
        chosen: list[WindowInfo] = []
        for q in args.windows:
            ranked = sorted(
                (
                    (r, i, w)
                    for i, w in enumerate(cands)
                    if (r := match_rank(w, q)) is not None and w not in chosen
                ),
                key=lambda t: (t[0], t[1]),
            )
            if not ranked:
                print(f"no window matches {q!r}. Candidates:")
                for c in cands:
                    print(f"   {c.label}  [{c.cls}]")
                return None
            chosen.append(ranked[0][2])
    elif args.pick:
        for i, c in enumerate(cands, 1):
            d = display_of(c, ds)
            print(f"  {i:2}. {c.label}{' (minimized)' if c.minimized else ''}  on {d.short if d else '?'}")
        chosen = [cands[int(input(f"Window for {d.short}: number? ")) - 1] for d in ds]
    else:
        chosen = []
        for d in ds:
            w = choose_target(d, None, cands, ds)
            if w is None:
                print(f"No eligible window on {d.short}. Put a window there or use --windows / --pick.")
                return None
            chosen.append(w)
        return chosen
    if not args.no_place:
        for w, d in zip(chosen, ds, strict=True):
            wf.place_window(w.hwnd, work_area(d, mons))
        time.sleep(0.4)
        chosen = [wf.window_info(w.hwnd) for w in chosen]
    return chosen


def header_lines(args, ds, chosen, elevated) -> list[str]:
    lines = [
        "=== LookFocus focus test ===",
        f"Windows {camera_sharing.windows_build()}; test process elevated: {elevated}; console-less: "
        f"{args.child_log is not None}; foreground lock timeout: {wf.foreground_lock_timeout_ms()} ms; "
        f"Alt fallback: {not args.no_alt}; first stage: {args.first_stage}",
        "targets:",
    ]
    for w, d in zip(chosen, ds, strict=True):
        actual = display_of(w, ds)
        warn = (
            ""
            if actual and actual.id == d.id
            else f"  (WARNING: its centre is on {actual.short if actual else '?'})"
        )
        lines.append(f"  {d.short}: {w.label} class={w.cls} elevated={w.elevated}{warn}")
    return lines


def run_switches(args, ds, chosen, elevated) -> None:
    header = header_lines(args, ds, chosen, elevated)
    for line in header:
        out(line)
    out("Starting in 2 s (hands off the keyboard and mouse).")
    time.sleep(2)
    results = []
    expected = wf.cursor_pos()
    aborted = ""
    n = 0
    for _cycle in range(args.cycles):
        for w, d in zip(chosen, ds, strict=True):
            n += 1
            minimized_first = bool(args.minimize_every) and n % args.minimize_every == 0
            if minimized_first:
                wf.minimize(w.hwnd)
                time.sleep(0.4)
            act = wf.focus_window(w.hwnd, allow_alt=not args.no_alt, first_stage=args.first_stage)
            time.sleep(0.1)
            kbd = wf.keyboard_focus_in(w.hwnd) if act.ok else False
            if args.move_cursor and act.ok:
                wf.move_cursor_into(d.rect)
                expected = wf.cursor_pos()
            results.append((n, d, w, act, kbd, minimized_first))
            tried = ", ".join(f"{s.name} {'OK' if s.ok else 'x'} {s.ms:.0f}ms" for s in act.tried) or "-"
            status = f"OK via {act.stage}" if act.ok else f"FAILED: {act.reason}"
            out(
                f"#{n:02} -> {d.short} {w.label}: {status} | kbd focus={'yes' if kbd else 'NO'} | {tried}"
                + (" | (minimized first)" if minimized_first else "")
            )
            t_end = time.monotonic() + args.dwell
            while time.monotonic() < t_end and not aborted:
                x, y = wf.cursor_pos()
                if (
                    abs(x - expected[0]) > ABORT_PX
                    or abs(y - expected[1]) > ABORT_PX
                    or wf.mouse_buttons_down()
                ):
                    aborted = "mouse moved / clicked"
                time.sleep(0.03)
            if aborted:
                break
        if aborted:
            break
    report(header, results, aborted)


def report(header, results, aborted) -> None:
    lines = ["", *header, ""]
    if aborted:
        lines.append(f"ABORTED after {len(results)} switches ({aborted}).")
    switches = [r for r in results if r[3].stage != "already"]
    lines.append(
        f"switches: {len(switches)} (plus {len(results) - len(switches)} where the target already had focus)"
    )
    by_target = defaultdict(list)
    for r in switches:
        by_target[(r[1].short, r[2].label)].append(r)
    lines.append(f"{'target':<62}{'ok':>7}{'kbd':>6}{'median ms':>11}  winning stages")
    for (dshort, label), rs in by_target.items():
        ok = [r for r in rs if r[3].ok]
        kbd = sum(1 for r in ok if r[4])
        med = statistics.median(r[3].total_ms for r in ok) if ok else float("nan")
        wins = Counter(r[3].stage for r in ok)
        lines.append(
            f"{(dshort + ' ' + label)[:61]:<62}{len(ok):>3}/{len(rs):<3}{kbd:>6}{med:>11.0f}  "
            + (", ".join(f"{k}={v}" for k, v in wins.most_common()) or "-")
        )
    tried, won = Counter(), Counter()
    for r in switches:
        for s in r[3].tried:
            tried[s.name] += 1
            won[s.name] += s.ok
    lines.append("per stage (how often each was tried / how often it was the one that worked):")
    for st in STAGES:
        if tried[st]:
            lines.append(
                f"  {st:<22} tried {tried[st]:>3}  worked {won[st]:>3}  ({100 * won[st] / tried[st]:.0f}%)"
            )
    reasons = Counter(r[3].reason for r in switches if not r[3].ok)
    if reasons:
        lines.append("failures:")
        lines += [f"  {k}x {reason}" for reason, k in reasons.items()]
    for line in lines:
        out(line)
    saved = paths.data_dir() / "focus_test_last.txt"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text("\n".join(lines), encoding="utf-8")
    out(f"\n(report also saved to {saved})")


def child_argv(args, chosen, log_path: Path) -> list[str]:
    argv = [
        str(Path(__file__).resolve()),
        "--zones",
        str(args.zones),
        "--cycles",
        str(args.cycles),
        "--dwell",
        str(args.dwell),
        "--first-stage",
        args.first_stage,
        "--child-log",
        str(log_path),
        "--hwnds",
        *[str(w.hwnd) for w in chosen],
    ]
    if args.minimize_every:
        argv += ["--minimize-every", str(args.minimize_every)]
    if args.no_alt:
        argv.append("--no-alt")
    if args.move_cursor:
        argv.append("--move-cursor")
    return argv


def run_in_child(args, chosen) -> int:
    """Run the switching loop in a console-less pythonw process (elevated if asked) and stream its log."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        print(f"{pythonw} not found; use --console")
        return 1
    log_path = Path(tempfile.gettempdir()) / f"lookfocus_focus_test_{int(time.time())}.log"
    log_path.write_text("", encoding="utf-8")
    argv = child_argv(args, chosen, log_path)
    if args.elevated and not wf.we_are_elevated():
        handle = wf.start_elevated(str(pythonw), argv)
        if handle is None:
            print("Elevation was cancelled or failed.")
            return 1
        running = lambda: wf.process_running(handle)  # noqa: E731
    else:
        proc = subprocess.Popen([str(pythonw), *argv])
        running = lambda: proc.poll() is None  # noqa: E731
    pos = 0
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        while True:
            alive = running()
            f.seek(pos)
            chunk = f.read()
            pos = f.tell()
            if chunk:
                print(chunk, end="", flush=True)
            if not alive:
                break
            time.sleep(0.1)
    log_path.unlink(missing_ok=True)
    return 0


def main() -> int:
    global _log
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zones", type=int, default=0)
    ap.add_argument(
        "--cycles", type=int, default=12, help="rounds through all displays (2 x 12 = 24 switches)"
    )
    ap.add_argument("--windows", nargs="+", metavar="QUERY")
    ap.add_argument("--pick", action="store_true")
    ap.add_argument("--no-place", action="store_true", help="don't move chosen windows into their displays")
    ap.add_argument(
        "--minimize-every",
        type=int,
        default=0,
        metavar="K",
        help="minimize the target before every K-th switch (exercises the restore stage)",
    )
    ap.add_argument("--no-alt", action="store_true", help="never use the Alt-tap last resort")
    ap.add_argument("--first-stage", choices=STAGES, default="restore")
    ap.add_argument("--dwell", type=float, default=0.8, help="seconds to stay on each window")
    ap.add_argument("--move-cursor", action="store_true", help="also move the pointer to the target")
    ap.add_argument("--elevated", action="store_true", help="run the switching as administrator (UAC prompt)")
    ap.add_argument(
        "--console", action="store_true", help="switch from this terminal process (biased, D-029)"
    )
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--child-log", type=Path, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--hwnds", type=int, nargs="+", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    monitors.enable_dpi_awareness()
    mons = monitors.list_monitors()
    try:
        ds = build_displays(mons, args.zones)
        require_multiple(ds, args.zones)
    except LayoutError as e:
        print(f"LookFocus: {e}")
        return 2

    if args.child_log is not None:  # console-less child: windows already chosen by the parent
        _log = args.child_log.open("a", encoding="utf-8")
        try:
            run_switches(args, ds, [wf.window_info(h) for h in args.hwnds], wf.we_are_elevated())
        except Exception:
            out(traceback.format_exc())
        return 0

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    chosen = choose_windows(args, ds, mons)
    if not chosen:
        return 1
    mode = "in this terminal (--console)" if args.console else "in a console-less child process, like the app"
    print(f"Switching will run {mode}{' as administrator' if args.elevated else ''}. Targets:")
    for line in header_lines(args, ds, chosen, wf.we_are_elevated())[3:]:
        print(line)
    if not args.yes:
        input("\nDon't type during the test; move the mouse to abort. Press Enter to start... ")
    if args.console:
        if args.elevated and not wf.we_are_elevated():
            print("--elevated with --console: run this terminal as administrator instead.")
            return 1
        run_switches(args, ds, chosen, wf.we_are_elevated())
        return 0
    return run_in_child(args, chosen)


if __name__ == "__main__":
    sys.exit(main())
