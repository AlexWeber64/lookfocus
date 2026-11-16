"""Input activity on Wayland: the Linux InputPoller.

Feeds the same pure InputTracker as Windows, from different sources:
- when the last input happened: the Wayland idle-notify protocol (wayland.ActivityWatcher);
- the pointer position: KWin's cursorPosChanged, pushed by the bridge;
- mouse buttons: not observable without reading /dev/input, which LookFocus never does, so always "up".
  A click without pointer movement therefore reads as typing; it only lengthens the dwell, the same
  trade-off as mouse-wheel scrolling on Windows.

Two Wayland-specific adjustments, both measured against the ordering of events on Plasma 6.8:
- The idle "resumed" event and the bridge's cursor event for the same pointer movement arrive over two
  different sockets in either order. A new input is only handed to the tracker once it is INPUT_LAG_S old,
  so the matching cursor event has arrived and the input is attributed to the mouse.
- The bridge throttles cursor events (kwin.CURSOR_MS), so the last few pixels of a movement can go
  unreported; input within SLACK_S of the last *seen* pointer change counts as mouse, not keyboard.
"""

from __future__ import annotations

import logging
import time

from lookfocus.inputactivity import InputPoller, InputTracker
from lookfocus.linux.kwin import get_bridge
from lookfocus.linux.wayland import ActivityWatcher

log = logging.getLogger(__name__)

INPUT_LAG_S = 0.02
SLACK_S = 0.15


class LinuxInputPoller(InputPoller):
    def __init__(self, interval: float = 0.04, clock=time.monotonic) -> None:
        super().__init__(interval=interval, clock=clock)
        self.tracker = InputTracker(slack_s=SLACK_S)
        self._watcher: ActivityWatcher | None = None

    def reset_cursor(self) -> None:
        with self._lock:
            self.tracker.reset_cursor(get_bridge().cursor)

    def stop(self) -> None:
        super().stop()
        if self._watcher is not None:
            self._watcher.stop()

    def run(self) -> None:
        bridge = get_bridge()
        watcher = self._watcher = ActivityWatcher(clock=self.clock)
        watcher.start()
        watcher.ready.wait(3)
        if watcher.error:
            # No idle source: typing can't be noticed, so the typing guard is off (the mouse guard works).
            log.warning("no input-activity source (%s); the typing guard is off", watcher.error)
        reported = self.clock()
        failures = 0
        while not self._halt.is_set():
            try:
                now = self.clock()
                if not watcher.error:
                    seen = watcher.last_input()
                    if now - seen >= INPUT_LAG_S:
                        reported = seen
                    idle_s = now - reported
                else:
                    idle_s = 1e9
                with self._lock:
                    self._state = self.tracker.update(now, idle_s, bridge.cursor, False)
                failures = 0
            except Exception:  # a dead input thread would silently disable the mouse/typing guards
                failures += 1
                if failures in (1, 10) or failures % 1000 == 0:
                    log.exception("input polling failed (%d in a row)", failures)
            self._halt.wait(self.interval)
