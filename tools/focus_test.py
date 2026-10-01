"""Cycle keyboard focus between one window per logical display and report which activation stage worked.

LookFocus runs in the background, where Windows' foreground lock blocks a plain SetForegroundWindow. This
tool runs the same escalation ladder as the app (targeting.activate via windows_focus.focus_window),
verifies every switch with GetForegroundWindow, checks that keyboard focus really landed in the target,
and reports per-stage success rates.

Windows per display:
  default        the topmost window already on each display / zone
  --windows Q..  the first window whose title / exe / class contains each query (one per display, in
                 display order), moved into its display unless --no-place
  --pick         choose from a numbered list (moved into place unless --no-place)

Zone mode on one monitor:  --zones 2 --windows "terminal" "chrome"
Elevated windows: open e.g. "Windows PowerShell (Admin)" and include it; a non-elevated run should fail
with a clear UIPI message. --elevated relaunches this test as administrator (UAC prompt) to compare.

Don't type during the test. Move the mouse (or click) to abort.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import lookfocus  # noqa: F401

# isort: split
from lookfocus import camera_sharing, monitors, paths
from lookfocus import windows_focus as wf
from lookfocus.displays import LayoutError, LogicalDisplay, Rect, build_displays, require_multiple
from lookfocus.targeting import STAGES, WindowInfo, display_of

ABORT_PX = 40


def work_area(d: LogicalDisplay, mons) -> Rect:
    m = next((m for m in mons if m.key == d.monitor_key), None)
    if m is None:
        return d.rect
    w, r = m.work, d.rect
    return Rect(max(w.left, r.left), max(w.top, r.top), min(w.right, r.right), min(w.bottom, r.bottom))


def matches(w: WindowInfo, q: str) -> bool:
    q = q.casefold()
    return q in w.title.casefold() or q in w.exe.casefold() or q in w.cls.casefold()


def choose_windows(args, ds, mons) -> list[WindowInfo] | None:
    cands = wf.candidate_windows()
    if args.windows:
        if len(args.windows) != len(ds):
            print(f"--windows needs {len(ds)} queries (one per display), got {len(args.windows)}")
            return None
        chosen = []
        for q in args.windows:
            w = next((w for w in cands if matches(w, q) and w not in chosen), None)
            if w is None:
                print(f"no window matches {q!r}. Candidates:")
                for c in cands:
                    print(f"   {c.label}  [{c.cls}]")
                return None
            chosen.append(w)
    elif args.pick:
        for i, c in enumerate(cands, 1):
            d = display_of(c, ds)
            extra = " (minimized)" if c.minimized else ""
            print(f"  {i:2}. {c.label}{extra}  on {d.short if d else '?'}")
        chosen = []
        for d in ds:
            n = int(input(f"Window for {d.short}: number? "))
            chosen.append(cands[n - 1])
    else:
        from lookfocus.targeting import choose_target

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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zones", type=int, default=0)
    ap.add_argument(
        "--cycles", type=int, default=12, help="rounds through all displays (2 displays x 12 = 24)"
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
    ap.add_argument("--elevated", action="store_true", help="relaunch this test as administrator")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    elevated = wf.we_are_elevated()
    if args.elevated and not elevated:
        argv = [a for a in sys.argv if a != "--elevated"] + ["--yes"]
        argv[0] = str(Path(__file__).resolve())
        if wf.relaunch_elevated(argv):
            print("Relaunched as administrator in a new window (results will also be saved to a file).")
            return 0
        print("Elevation was cancelled or failed.")
        return 1

    monitors.enable_dpi_awareness()
    mons = monitors.list_monitors()
    try:
        ds = build_displays(mons, args.zones)
        require_multiple(ds, args.zones)
    except LayoutError as e:
        print(f"LookFocus: {e}")
        return 2

    chosen = choose_windows(args, ds, mons)
    if not chosen:
        return 1
    header = [
        "=== LookFocus focus test ===",
        f"Windows {camera_sharing.windows_build()}; this process elevated: {elevated}; "
        f"foreground lock timeout: {wf.foreground_lock_timeout_ms()} ms; Alt fallback: {not args.no_alt}; "
        f"first stage: {args.first_stage}",
        "targets:",
    ]
    for w, d in zip(chosen, ds, strict=True):
        actual = display_of(w, ds)
        where = (
            ""
            if actual and actual.id == d.id
            else f"  (WARNING: its centre is on {actual.short if actual else '?'})"
        )
        header.append(f"  {d.short}: {w.label} class={w.cls} elevated={w.elevated}{where}")
    print("\n".join(header))
    if not args.yes:
        input("\nDon't type during the test; move the mouse to abort. Press Enter to start... ")
    print("Starting in 2 s (hands off the keyboard and mouse).")
    time.sleep(2)

    results = []
    expected = wf.cursor_pos()
    aborted = ""
    n = 0
    for _cycle in range(args.cycles):
        for w, d in zip(chosen, ds, strict=True):
            n += 1
            minimized_first = args.minimize_every and n % args.minimize_every == 0
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
            print(
                f"#{n:02} -> {d.short} {w.label}: {status} | kbd focus={'yes' if kbd else 'NO'} | {tried}"
                + (" | (minimized first)" if minimized_first else ""),
                flush=True,
            )
            t_end = time.monotonic() + args.dwell
            while time.monotonic() < t_end:
                x, y = wf.cursor_pos()
                if (
                    abs(x - expected[0]) > ABORT_PX
                    or abs(y - expected[1]) > ABORT_PX
                    or wf.mouse_buttons_down()
                ):
                    aborted = "mouse moved / clicked"
                    break
                time.sleep(0.03)
            if aborted:
                break
        if aborted:
            break

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
    tried = Counter()
    won = Counter()
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
        for reason, k in reasons.items():
            lines.append(f"  {k}x {reason}")
    report = "\n".join(lines)
    print(report)
    out = paths.data_dir() / "focus_test_last.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"\n(report also saved to {out})")
    if elevated and args.yes:
        input("Press Enter to close this window... ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
