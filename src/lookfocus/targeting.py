"""Which window to focus on a logical display, and the activation escalation ladder.

Pure logic: no Win32 imports. windows_focus.py supplies the real FocusApi; tests supply a fake.

Target selection: the window that was last active on the display (remembered by a foreground tracker)
if it is still eligible and still on that display, else the topmost eligible window there (z-order).
Minimized windows are not targets: they aren't "on" the display, and restoring a window the user
minimized just because they glanced at its monitor would be surprising (D-027). Nothing eligible → None.

Activation: Windows' foreground lock stops background processes from taking focus, and the APIs' return
values can't be trusted, so every stage is verified by watching the foreground window (up to
`stage_timeout`) before moving on:
    restore (only if minimized) -> set_foreground -> attach_thread_input
    -> input_unassigned_vk -> input_alt (last resort: a lone Alt tap can open menu bars)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from lookfocus.displays import LogicalDisplay, Rect, display_for_point

STAGES = ("restore", "set_foreground", "attach_thread_input", "input_unassigned_vk", "input_alt")
VK_UNASSIGNED = 0xE8  # documented as unassigned; also what AutoHotkey uses as its "mask key"
VK_MENU = 0x12  # Alt


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    cls: str
    pid: int
    exe: str
    rect: Rect  # visible frame (DWM extended frame bounds); normal position if minimized
    minimized: bool = False
    elevated: bool | None = None  # None = couldn't tell (usually: a higher-integrity process)

    @property
    def label(self) -> str:
        t = self.title if len(self.title) <= 40 else self.title[:37] + "..."
        return f"{t!r} ({self.exe or self.cls})"


def display_of(w: WindowInfo, displays: list[LogicalDisplay]) -> LogicalDisplay | None:
    """Logical display holding the window's centre (nearest one if the centre is off-screen)."""
    return display_for_point(displays, *w.rect.center)


def choose_target(
    display: LogicalDisplay,
    remembered: int | None,
    windows: list[WindowInfo],
    displays: list[LogicalDisplay],
) -> WindowInfo | None:
    """`windows`: eligible top-level windows in z-order (topmost first)."""
    by_hwnd = {w.hwnd: w for w in windows}
    w = by_hwnd.get(remembered) if remembered is not None else None
    if (
        w is not None
        and not w.minimized
        and (d := display_of(w, displays)) is not None
        and d.id == display.id
    ):
        return w
    for w in windows:
        if w.minimized:
            continue
        d = display_of(w, displays)
        if d is not None and d.id == display.id:
            return w
    return None


class FocusApi(Protocol):
    def foreground(self) -> int: ...
    def same_window(self, a: int, b: int) -> bool: ...
    def is_iconic(self, hwnd: int) -> bool: ...
    def restore(self, hwnd: int) -> None: ...
    def set_foreground(self, hwnd: int) -> bool: ...
    def attach_and_activate(self, hwnd: int) -> str: ...
    def tap_key(self, vk: int) -> bool: ...
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class StageResult:
    name: str
    ok: bool
    ms: float  # time until the window became foreground, or the full wait if it didn't
    detail: str = ""


@dataclass
class Activation:
    ok: bool
    stage: str | None  # the stage that worked, "already" if it was foreground, None on failure
    tried: list[StageResult] = field(default_factory=list)
    reason: str = ""

    @property
    def total_ms(self) -> float:
        return sum(s.ms for s in self.tried)


def activate(
    api: FocusApi,
    hwnd: int,
    *,
    stage_timeout: float = 0.5,
    poll: float = 0.015,
    allow_alt: bool = True,
    first_stage: str = "restore",
) -> Activation:
    """Run the ladder until `hwnd` is verifiably the foreground window. `first_stage` lets a caller skip
    stages it has learned never work on this machine (restore still runs if the window is minimized)."""
    if api.same_window(api.foreground(), hwnd):
        return Activation(True, "already")
    result = Activation(False, None)

    def verify(name: str, detail: str = "") -> bool:
        t0 = api.now()
        while True:
            if api.same_window(api.foreground(), hwnd):
                result.tried.append(StageResult(name, True, (api.now() - t0) * 1000, detail))
                result.ok, result.stage = True, name
                return True
            if api.now() - t0 >= stage_timeout:
                result.tried.append(StageResult(name, False, (api.now() - t0) * 1000, detail))
                return False
            api.sleep(poll)

    start = STAGES.index(first_stage) if first_stage in STAGES else 0
    if api.is_iconic(hwnd):
        api.restore(hwnd)
        if verify("restore"):
            return result
    if start <= 1:
        ok = api.set_foreground(hwnd)
        if verify("set_foreground", f"returned {ok}"):
            return result
    if start <= 2:
        detail = api.attach_and_activate(hwnd)
        if verify("attach_thread_input", detail):
            return result
    if start <= 3:
        sent = api.tap_key(VK_UNASSIGNED)
        ok = api.set_foreground(hwnd)
        if verify("input_unassigned_vk", f"sent={sent} set_foreground={ok}"):
            return result
    if allow_alt:
        sent = api.tap_key(VK_MENU)
        ok = api.set_foreground(hwnd)
        if verify("input_alt", f"sent={sent} set_foreground={ok}"):
            return result
    result.reason = "no stage brought the window to the foreground"
    return result
