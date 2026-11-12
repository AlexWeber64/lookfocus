"""Which KWin windows are focus targets, from the bridge's window snapshots.

Pure logic (no DBus, no Qt): unit tested with recorded snapshots. The rules mirror the Win32 Alt-Tab-style
filter in windows_focus.is_candidate: a managed, normal (or dialog) app window on the current virtual
desktop and activity, shown in the task switcher, not a transient (it is reached through its main window,
whose modal dialog the script activates instead), and not one of LookFocus's own windows.

Window ids are KWin's internalId strings (they take the place of Win32 HWNDs in targeting.WindowInfo).
Geometry is in KWin's logical pixels, the same space as the screen rectangles (monitors.py).
"""

from __future__ import annotations

import os

from lookfocus.displays import Rect
from lookfocus.targeting import WindowInfo


def window_info(d: dict) -> WindowInfo:
    x, y, w, h = (float(d.get(k) or 0) for k in ("x", "y", "w", "h"))
    return WindowInfo(
        hwnd=d["id"],
        title=d.get("caption") or "",
        cls=d.get("cls") or "",
        pid=int(d.get("pid") or 0),
        exe=d.get("app") or d.get("cls") or "",
        rect=Rect(round(x), round(y), round(x + w), round(y + h)),
        minimized=bool(d.get("minimized")),
        elevated=None,  # no UIPI-style barrier between a user's own windows on Wayland
    )


def is_candidate(d: dict, own_pid: int | None = None) -> bool:
    if not d.get("managed", True) or d.get("deleted"):
        return False
    if int(d.get("pid") or 0) == (os.getpid() if own_pid is None else own_pid):
        return False
    if d.get("desktopWindow") or d.get("dock") or d.get("special") or d.get("popup"):
        return False
    if not (d.get("normal") or d.get("dialog")):
        return False
    if d.get("skipSwitcher"):  # e.g. xwaylandvideobridge, KWin's own helper windows
        return False
    if d.get("transient") and d.get("root") not in (None, d.get("id")):
        return False
    if not d.get("onCurrentDesktop", True):
        return False
    if d.get("hidden") and not d.get("minimized"):
        return False
    return bool(d.get("minimized")) or (float(d.get("w") or 0) > 1 and float(d.get("h") or 0) > 1)


def candidate_windows(snapshot: dict, own_pid: int | None = None) -> list[WindowInfo]:
    """Eligible windows, topmost first (the snapshot's stacking order is bottom to top)."""
    return [window_info(d) for d in reversed(snapshot.get("windows") or []) if is_candidate(d, own_pid)]


def find(snapshot: dict, window_id: str) -> dict | None:
    return next((d for d in snapshot.get("windows") or [] if d.get("id") == window_id), None)


def root_id(snapshot: dict, window_id: str | None) -> str | None:
    if not window_id:
        return None
    d = find(snapshot, window_id)
    return (d.get("root") or window_id) if d else window_id
