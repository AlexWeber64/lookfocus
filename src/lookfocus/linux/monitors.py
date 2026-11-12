"""Physical monitors on KDE Plasma (Wayland), from KWin's screen list via the bridge.

Coordinates are KWin's logical pixels (physical pixels divided by each screen's scale factor): the space
KWin reports window geometry and the pointer in, so displays, windows and the pointer always agree. The
Windows build needs per-monitor DPI awareness for the same guarantee; here there is nothing to enable.
"""

from __future__ import annotations

import re

from lookfocus.displays import Monitor, Rect
from lookfocus.linux.kwin import get_bridge

# Connector names of built-in panels (they get the "built-in display" label, like unnamed Windows panels).
_INTERNAL = re.compile(r"^(eDP|LVDS|DSI)-", re.I)


def enable_dpi_awareness() -> str:
    return "logical pixels (Wayland)"


def monitor_from(screen: dict, primary: bool) -> Monitor:
    """Pure: one KWin screen record -> Monitor."""
    name = screen.get("name") or ""
    x, y = round(screen.get("x", 0)), round(screen.get("y", 0))
    rect = Rect(x, y, x + round(screen.get("w", 0)), y + round(screen.get("h", 0)))
    maker, model, serial = (screen.get(k) or "" for k in ("manufacturer", "model", "serial"))
    friendly = "" if _INTERNAL.match(name) else " ".join(p for p in (maker, model) if p)
    return Monitor(
        device=name,
        path="|".join((maker, model, serial)) if (maker or model or serial) else "",
        name=friendly,
        rect=rect,
        work=rect,
        primary=primary,
        dpi=round(96 * float(screen.get("scale") or 1.0)),
    )


def monitors_from(info: dict) -> list[Monitor]:
    order = info.get("order") or []
    primary = order[0] if order else None
    screens = info.get("screens") or []
    if primary is None and screens:
        primary = screens[0].get("name")
    return [monitor_from(s, s.get("name") == primary) for s in screens]


def list_monitors() -> list[Monitor]:
    return monitors_from(get_bridge().request("screens"))
