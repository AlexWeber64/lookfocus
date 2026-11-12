"""Linux side of focus switching (the counterpart of windows_focus.py): window list, the foreground
tracker, activation and the pointer, all through the KWin bridge.

There is no activation ladder here. Windows' foreground lock needs escalating tricks; a KWin script sets
`workspace.activeWindow` directly, and keyboard focus follows the active window on Wayland. The result is
still verified against KWin's active window, as on Windows, never assumed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from lookfocus.displays import LogicalDisplay, Rect
from lookfocus.linux import windowlist
from lookfocus.linux.kwin import BridgeError, get_bridge
from lookfocus.targeting import Activation, StageResult, WindowInfo, choose_target, display_of

log = logging.getLogger(__name__)

STAGE = "kwin"


def snapshot() -> dict:
    return get_bridge().request("windows")


def candidate_windows(own_pid: int | None = None) -> list[WindowInfo]:
    return windowlist.candidate_windows(snapshot(), own_pid)


def foreground_window() -> WindowInfo | None:
    """The active window if it is a focus target (its main window, if a dialog is active)."""
    snap = snapshot()
    root = windowlist.root_id(snap, snap.get("active"))
    d = windowlist.find(snap, root) if root else None
    return windowlist.window_info(d) if d and windowlist.is_candidate(d) else None


def focus_window(
    window_id: str, *, allow_alt: bool = True, first_stage: str = STAGE, stage_timeout: float = 0.5
) -> Activation:
    """Activate `window_id` (or its modal dialog) through KWin and verify it became active.
    `allow_alt` and `first_stage` exist for the Windows ladder's sake and are ignored."""
    bridge = get_bridge()
    t0 = time.perf_counter()
    try:
        r = bridge.request("activate", id=window_id)
    except BridgeError as e:
        return Activation(False, None, reason=str(e))
    if r.get("reason"):
        return Activation(False, None, reason=r["reason"])
    ok = bool(r.get("ok"))
    end = time.monotonic() + stage_timeout
    while not ok and time.monotonic() < end:  # the activation may land a moment later
        time.sleep(0.015)
        ok = bridge.active == window_id
    ms = (time.perf_counter() - t0) * 1000
    act = Activation(ok, STAGE if ok else None, [StageResult(STAGE, ok, ms, f"active={r.get('active')}")])
    if not ok:
        act.reason = "KWin did not make the window active"
        log.warning("couldn't focus %s: %s", window_id, act.reason)
    return act


_cursor_warned = False


def move_cursor_into(rect: Rect) -> None:
    """Wayland lets no client (and no KWin script) warp the pointer: move_cursor_on_switch is a no-op."""
    global _cursor_warned
    if not _cursor_warned:
        _cursor_warned = True
        log.warning("move_cursor_on_switch isn't possible on Wayland; ignoring it")


def cursor_pos() -> tuple[int, int]:
    return get_bridge().cursor


def mouse_buttons_down() -> bool:
    """Not observable on Wayland without reading input devices, which LookFocus never does (D-050)."""
    return False


def we_are_elevated() -> bool:
    return False


def start_elevated(exe: str, argv: list[str], show: int = 0) -> None:
    return None


class ForegroundTracker(threading.Thread):
    """Remembers the last active eligible window per display. Event-driven: KWin's windowActivated reaches
    this thread through the bridge (no polling), with a slow safety re-check every `interval` s."""

    def __init__(self, displays: Callable[[], list[LogicalDisplay]], interval: float = 2.0) -> None:
        super().__init__(name="foreground-tracker", daemon=True)
        self.displays = displays
        self.interval = interval
        self._last: dict[str, str] = {}
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._changed = threading.Event()

    def stop(self) -> None:
        self._halt.set()
        self._changed.set()
        self.join(2)

    def remembered(self, display_id: str) -> str | None:
        with self._lock:
            return self._last.get(display_id)

    def note(self, window_id: str, snap: dict | None = None) -> None:
        snap = snapshot() if snap is None else snap
        root = windowlist.root_id(snap, window_id)
        d = windowlist.find(snap, root) if root else None
        if d is None or not windowlist.is_candidate(d):
            return
        info = windowlist.window_info(d)
        disp = display_of(info, self.displays())
        if disp is not None:
            with self._lock:
                if self._last.get(disp.id) != root:
                    log.debug("last active on %s: %s", disp.short, info.label)
                self._last[disp.id] = root

    def target_for(self, display: LogicalDisplay) -> WindowInfo | None:
        return choose_target(display, self.remembered(display.id), candidate_windows(), self.displays())

    def _on_bridge(self, kind: str, _data: dict) -> None:
        if kind == "active":
            self._changed.set()

    def run(self) -> None:
        bridge = get_bridge()
        bridge.add_listener(self._on_bridge)
        last = None
        try:
            while not self._halt.is_set():
                self._changed.wait(self.interval)
                self._changed.clear()
                if self._halt.is_set():
                    break
                try:
                    snap = snapshot()
                    active = windowlist.root_id(snap, snap.get("active"))
                    if active and active != last:
                        last = active
                        self.note(active, snap)
                except Exception:
                    log.exception("foreground tracking failed")
        finally:
            bridge.remove_listener(self._on_bridge)
