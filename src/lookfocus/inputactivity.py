"""Mouse and keyboard *activity* (not content), from polling only.

Never installs hooks and never reads which key was pressed. Every ~40 ms it reads:
- GetLastInputInfo: when the last input of any kind happened;
- GetCursorPos: whether the pointer moved;
- GetAsyncKeyState for VK_LBUTTON / VK_RBUTTON / VK_MBUTTON only: whether a mouse button is down.
Keyboard activity is *inferred*: input that happened after the mouse was last touched. Known blind spot:
mouse-wheel scrolling moves no pointer, so it reads as typing (that only lengthens the dwell).

InputTracker is pure (unit tested); InputPoller is the Windows thread feeding it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Pointer moves smaller than this (px) don't count as mouse *use*: keep-awake jigglers ("Move Mouse"
# on the dev machine) nudge the pointer by a few pixels. They still explain the input event, so they're
# never mistaken for typing.
MOVE_THRESHOLD_PX = 4
# Input this soon after the pointer was touched is attributed to the mouse (polling granularity).
SLACK_S = 0.06
# `now - idle` is recomputed every poll, so the same input event drifts by float noise; GetLastInputInfo
# has 1 ms resolution, so anything within 5 ms is the event already seen.
NEW_INPUT_EPS_S = 0.005


@dataclass(frozen=True)
class InputState:
    mouse_active_at: float = float("-inf")  # last real pointer move or button press
    keyboard_at: float = float("-inf")  # last inferred keyboard input


class InputTracker:
    def __init__(self, move_threshold_px: int = MOVE_THRESHOLD_PX) -> None:
        self.move_threshold = move_threshold_px
        self._cursor: tuple[int, int] | None = None
        self._touched_at = float("-inf")  # any pointer change or button, including jitter
        self._mouse_at = float("-inf")
        self._key_at = float("-inf")
        self._ignore_from = float("inf")
        self._ignore_until = float("-inf")
        self._input_seen = float("-inf")

    def ignore(self, start: float, end: float) -> None:
        """LookFocus itself injects input (a key tap during activation): don't count input in [start, end]."""
        self._ignore_from, self._ignore_until = start, end

    def reset_cursor(self, pos: tuple[int, int]) -> None:
        """LookFocus moved the pointer itself: take the new position as the baseline."""
        self._cursor = pos

    def update(self, now: float, idle_s: float, cursor: tuple[int, int], buttons_down: bool) -> InputState:
        """`idle_s`: seconds since the last input event (from GetLastInputInfo)."""
        last_input = now - idle_s
        if self._cursor is None:  # first poll: baseline only; whatever happened before is unknown
            self._cursor, self._input_seen = cursor, last_input
            return InputState(self._mouse_at, self._key_at)
        if cursor != self._cursor:
            self._touched_at = now
            dx, dy = cursor[0] - self._cursor[0], cursor[1] - self._cursor[1]
            if max(abs(dx), abs(dy)) >= self.move_threshold:
                self._mouse_at = now
        self._cursor = cursor
        if buttons_down:
            self._touched_at = self._mouse_at = now
        if last_input > self._input_seen + NEW_INPUT_EPS_S:  # a new input event since the last poll
            self._input_seen = last_input
            injected = self._ignore_from <= last_input <= self._ignore_until
            if not injected and last_input > self._touched_at + SLACK_S:
                self._key_at = last_input
        return InputState(self._mouse_at, self._key_at)


class InputPoller(threading.Thread):
    """Polls the Win32 input state every `interval` s; `state()` is safe to call from any thread."""

    def __init__(self, interval: float = 0.04, clock=time.monotonic) -> None:
        super().__init__(name="input-activity", daemon=True)
        self.interval = interval
        self.clock = clock
        self.tracker = InputTracker()
        self._lock = threading.Lock()
        self._state = InputState()
        self._halt = threading.Event()

    def state(self) -> InputState:
        with self._lock:
            return self._state

    def ignore_for(self, seconds: float) -> None:
        now = self.clock()
        self.ignore_between(now - 0.05, now + seconds)

    def ignore_between(self, start: float, end: float) -> None:
        with self._lock:
            self.tracker.ignore(start, end)

    def reset_cursor(self) -> None:
        from lookfocus.windows_focus import cursor_pos

        with self._lock:
            self.tracker.reset_cursor(cursor_pos())

    def stop(self) -> None:
        self._halt.set()
        self.join(2)

    def run(self) -> None:
        import ctypes
        from ctypes import wintypes

        from lookfocus.windows_focus import cursor_pos, mouse_buttons_down

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
        user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
        user32.GetLastInputInfo.restype = wintypes.BOOL
        kernel32.GetTickCount.restype = wintypes.DWORD
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        failures = 0
        while not self._halt.is_set():
            try:
                if user32.GetLastInputInfo(ctypes.byref(lii)):
                    idle_ms = (kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF  # wraps every 49.7 days
                    now = self.clock()
                    with self._lock:
                        self._state = self.tracker.update(
                            now, idle_ms / 1000.0, cursor_pos(), mouse_buttons_down()
                        )
                failures = 0
            except Exception:  # a dead input thread would silently disable the mouse/typing guards
                failures += 1
                if failures in (1, 10) or failures % 1000 == 0:
                    log.exception("input polling failed (%d in a row)", failures)
            self._halt.wait(self.interval)
