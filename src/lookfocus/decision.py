"""When to move focus: the decision state machine.

Pure logic: no Win32, camera or clock calls. Every call passes `now` (seconds, monotonic), so tests drive it
with scripted pose streams and a fake clock.

Per tick (one per processed frame, and on idle ticks): the classifier's verdict for the current head pose
(a display id, "none" = facing no display, or no face at all), the input activity, and whether LookFocus is
paused. It returns a display id exactly when the display the user faces *changes* (after the guards), and
never repeats one.

Guards (from the spec):
- dwell: the new display must be faced for `dwell_s` (300 ms) so quick glances don't switch;
- typing: if the keyboard was used within `typing_grace_s`, the dwell is `typing_dwell_s` (1 s) so focus
  never jumps mid-word;
- mouse: no switching while the mouse moves or clicks, and for `mouse_grace_s` (1.5 s) after; the dwell
  only starts counting once the guard lifts;
- hysteresis: leaving the current display needs a bigger turn than entering one. Another display only
  counts when its classifier distance beats the current display's by `hysteresis` (normalized units), so
  poses near a bezel don't ping-pong;
- paused / no face: no switching. Brief gaps (a dropped frame, one stray classification) shorter than
  `gap_tolerance_s` don't reset a dwell in progress.
"none" (phone, desk) and no-face periods leave the current display unchanged: looking back at it doesn't
re-emit it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NONE = "none"  # classifier: facing no display


@dataclass(frozen=True)
class DecisionSettings:
    dwell_s: float = 0.30
    typing_dwell_s: float = 1.0
    typing_grace_s: float = 2.0
    mouse_grace_s: float = 1.5
    hysteresis: float = 2.0  # normalized classifier units (see classifier.py)
    gap_tolerance_s: float = 0.15


@dataclass(frozen=True)
class Observation:
    """One tick's input. `display`: classified display id, NONE, or None when no face was seen.
    `distances`: per-display normalized distances from the classifier (empty if unknown)."""

    display: str | None
    distances: dict[str, float] = field(default_factory=dict)
    mouse_active_at: float = float("-inf")  # last mouse move/click
    keyboard_at: float = float("-inf")  # last inferred keyboard input
    paused: bool = False


@dataclass
class DecisionMachine:
    settings: DecisionSettings = field(default_factory=DecisionSettings)
    current: str | None = None  # the display the user is considered to face (last emitted or seeded)
    state: str = "idle"  # idle | dwelling | mouse | paused | no_face | away (facing none)
    _pending: str | None = None
    _since: float = 0.0
    _last_seen: float = 0.0

    def seed(self, display: str | None) -> None:
        """Adopt a display without emitting (startup, resume): no switch until the user looks elsewhere."""
        self.current = display
        self._pending = None

    def candidate(self, obs: Observation) -> str | None:
        """Classified display after hysteresis (None when facing nothing or no face)."""
        d = obs.display
        if d is None or d == NONE:
            return None
        cur = self.current
        known = cur is not None and d != cur and cur in obs.distances and d in obs.distances
        if known and obs.distances[d] + self.settings.hysteresis >= obs.distances[cur]:
            return cur  # not a decisive enough turn away from the current display
        return d

    def update(self, now: float, obs: Observation) -> str | None:
        s = self.settings
        if obs.paused:
            self.state = "paused"
            self._pending = None
            return None
        if obs.display is None:
            self.state = "no_face"
            return self._gap(now)
        cand = self.candidate(obs)
        if cand is None:
            self.state = "away"
            return self._gap(now)
        if self.current is None and self._pending is None:
            # Nothing faced yet (first sight): adopt silently, like seed().
            self.current = cand
            self.state = "idle"
            return None
        if cand == self.current:
            if self._pending is not None and now - self._last_seen <= s.gap_tolerance_s:
                self.state = "dwelling"  # one stray frame back on the current display: keep the dwell
                return None
            self.state = "idle"
            self._pending = None
            return None
        if now - obs.mouse_active_at < s.mouse_grace_s:
            self.state = "mouse"
            self._pending, self._since, self._last_seen = cand, now, now  # dwell starts after the guard
            return None
        if self._pending != cand:
            if self._pending is not None and now - self._last_seen <= s.gap_tolerance_s:
                self.state = "dwelling"  # one stray frame: keep the dwell for the earlier candidate
                return None
            self._pending, self._since = cand, now
        self._last_seen = now
        dwell = s.typing_dwell_s if now - obs.keyboard_at < s.typing_grace_s else s.dwell_s
        if now - self._since >= dwell:
            self.current = cand
            self._pending = None
            self.state = "idle"
            return cand
        self.state = "dwelling"
        return None

    def _gap(self, now: float) -> None:
        if self._pending is not None and now - self._last_seen > self.settings.gap_tolerance_s:
            self._pending = None
        return None
