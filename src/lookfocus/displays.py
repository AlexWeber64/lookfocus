"""Logical displays: what LookFocus classifies you as facing and moves focus to.

A logical display is a whole physical monitor, or a vertical zone of one (``--zones N`` splits the primary
monitor into N side-by-side strips, for development with a single screen). Calibration, the classifier,
the decision logic and window targeting all work on logical displays and never on monitors directly.

Pure logic: no Win32 imports (monitors.py produces the Monitor records).
All coordinates are physical pixels in virtual-screen space (the process is per-monitor DPI aware).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[float, float]:
        return (self.left + self.right) / 2, (self.top + self.bottom) / 2

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def distance_to(self, x: float, y: float) -> float:
        dx = max(self.left - x, 0.0, x - (self.right - 1))
        dy = max(self.top - y, 0.0, y - (self.bottom - 1))
        return (dx * dx + dy * dy) ** 0.5

    def point_at(self, fx: float, fy: float) -> tuple[int, int]:
        """Pixel at fractional position (0..1, 0..1) inside the rect."""
        return round(self.left + fx * (self.width - 1)), round(self.top + fy * (self.height - 1))

    def split_vertical(self, n: int) -> list[Rect]:
        """n side-by-side strips, left to right, covering the rect exactly."""
        edges = [self.left + round(i * self.width / n) for i in range(n + 1)]
        return [Rect(edges[i], self.top, edges[i + 1], self.bottom) for i in range(n)]

    def as_list(self) -> list[int]:
        return [self.left, self.top, self.right, self.bottom]

    @classmethod
    def from_list(cls, v: list[int]) -> Rect:
        return cls(*(int(x) for x in v))


@dataclass(frozen=True)
class Monitor:
    """One physical monitor as Windows reports it."""

    device: str  # GDI device name, e.g. "\\\\.\\DISPLAY1" (can be renumbered)
    path: str  # monitor device path from QueryDisplayConfig (stable per monitor+port), or ""
    name: str  # friendly name, e.g. "DELL U2720Q"; internal panels often have none
    rect: Rect
    work: Rect
    primary: bool
    dpi: int = 96

    @property
    def key(self) -> str:
        """Calibration identity: device, resolution and position (rearranging needs a recalibration)."""
        r = self.rect
        return f"{self.path or self.device}|{r.width}x{r.height}@{r.left},{r.top}"


@dataclass(frozen=True)
class LogicalDisplay:
    id: str
    label: str
    rect: Rect
    monitor_key: str
    zone: int = 0  # 1-based zone number, 0 = whole monitor
    zones: int = 0

    @property
    def short(self) -> str:
        return self.label.split(" (")[0]


ZONE_WORDS = {
    2: ["left half", "right half"],
    3: ["left third", "middle third", "right third"],
    4: ["far left quarter", "left quarter", "right quarter", "far right quarter"],
}


class LayoutError(Exception):
    """The display layout can't be used as-is (e.g. only one display and no zones)."""


def build_displays(monitors: list[Monitor], zones: int = 0) -> list[LogicalDisplay]:
    """Logical displays ordered left to right (then top to bottom); zones split the primary monitor."""
    if zones == 1 or zones < 0:
        raise LayoutError("--zones must be 2 or more (or 0 for no zones)")
    ordered = sorted(monitors, key=lambda m: (m.rect.left, m.rect.top))
    out: list[LogicalDisplay] = []
    for num, m in enumerate(ordered, 1):
        gdi = m.device.removeprefix("\\\\.\\")  # "\\.\DISPLAY2" -> "DISPLAY2"
        name = m.name or ("built-in display" if m.primary and len(ordered) == 1 else gdi)
        base = f"Monitor {num}"
        if zones >= 2 and m.primary:
            words = ZONE_WORDS.get(zones)
            for i, r in enumerate(m.rect.split_vertical(zones), 1):
                where = words[i - 1] if words else f"zone {i} of {zones}"
                out.append(
                    LogicalDisplay(
                        id=f"{m.key}#zone{i}of{zones}",
                        label=f"Zone {i}/{zones} ({where} of {base}, {name})",
                        rect=r,
                        monitor_key=m.key,
                        zone=i,
                        zones=zones,
                    )
                )
        else:
            tag = ", primary" if m.primary and len(ordered) > 1 else ""
            out.append(
                LogicalDisplay(id=m.key, label=f"{base} ({name}{tag})", rect=m.rect, monitor_key=m.key)
            )
    return out


def require_multiple(displays: list[LogicalDisplay], zones: int) -> None:
    if len(displays) < 2:
        raise LayoutError(
            "Only one display found. LookFocus needs at least two displays to switch between. "
            "To try it on one screen, split it into zones, e.g. `--zones 2` (left and right halves)."
        )


def display_at(displays: list[LogicalDisplay], x: float, y: float) -> LogicalDisplay | None:
    for d in displays:
        if d.rect.contains(x, y):
            return d
    return None


def display_for_point(displays: list[LogicalDisplay], x: float, y: float) -> LogicalDisplay | None:
    """Display containing (x, y), else the nearest one (e.g. a window centre just off-screen)."""
    hit = display_at(displays, x, y)
    if hit is not None or not displays:
        return hit
    return min(displays, key=lambda d: d.rect.distance_to(x, y))


def layout_diff(calibrated_ids: list[str], displays: list[LogicalDisplay]) -> tuple[list[str], list[str]]:
    """(calibrated displays that are gone, current displays with no calibration)."""
    now = {d.id for d in displays}
    before = set(calibrated_ids)
    return sorted(before - now), sorted(now - before)
