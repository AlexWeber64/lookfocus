"""Decision state machine, driven by scripted pose streams on a fake clock (15 fps unless noted)."""

import pytest

from lookfocus.decision import NONE, DecisionMachine, DecisionSettings, Observation

FPS = 15
DT = 1 / FPS


class Script:
    """Feeds observations at 15 fps and records emitted events with their timestamps."""

    def __init__(self, machine=None, start="A", **settings):
        self.m = machine or DecisionMachine(DecisionSettings(**settings))
        self.m.seed(start)
        self.t = 0.0
        self.events: list[tuple[float, str]] = []
        self.mouse_at = float("-inf")
        self.key_at = float("-inf")
        self.paused = False

    def look(self, display, seconds, distances=None):
        for _ in range(round(seconds * FPS)):
            obs = Observation(display, distances or {}, self.mouse_at, self.key_at, self.paused)
            e = self.m.update(self.t, obs)
            if e is not None:
                self.events.append((round(self.t, 3), e))
            self.t += DT
        return self

    def mouse(self):
        self.mouse_at = self.t
        return self

    def typed(self):
        self.key_at = self.t
        return self

    @property
    def names(self):
        return [e for _, e in self.events]


class TestDwell:
    def test_switch_after_dwell(self):
        s = Script().look("A", 1).look("B", 1)
        assert s.names == ["B"]
        t_switch = s.events[0][0]
        assert 1.0 + 0.30 - 1e-9 <= t_switch <= 1.0 + 0.30 + DT  # ~300 ms after first facing B

    def test_quick_glance_is_ignored(self):
        assert Script().look("A", 1).look("B", 0.2).look("A", 1).names == []

    def test_event_only_on_change_never_repeated(self):
        s = Script().look("B", 2).look("B", 2)
        assert s.names == ["B"]

    def test_alternating(self):
        s = Script()
        for d in "BABAB":
            s.look(d, 1)
        assert s.names == list("BABAB")

    def test_three_displays(self):
        assert Script().look("B", 1).look("C", 1).look("A", 1).names == ["B", "C", "A"]


class TestTyping:
    def test_recent_typing_lengthens_dwell(self):
        s = Script().look("A", 1).typed().look("B", 0.6)
        assert s.names == []  # 0.6 s would be enough normally, not while typing
        s.look("B", 0.5)
        assert s.names == ["B"]
        assert s.events[0][0] == pytest.approx(1.0 + 1.0, abs=2 * DT)

    def test_typing_long_ago_is_normal_dwell(self):
        s = Script()
        s.typed().look("A", 3).look("B", 0.4)
        assert s.names == ["B"]


class TestMouse:
    def test_no_switch_while_mouse_moves(self):
        s = Script().look("A", 1)
        for _ in range(15):  # mouse moving for 1 s while facing B
            s.mouse().look("B", DT)
        assert s.names == []

    def test_switch_needs_grace_then_dwell_after_mouse_stops(self):
        s = Script().look("A", 1).mouse().look("B", 1.4)
        assert s.names == []  # still inside the 1.5 s grace
        s.look("B", 0.5)
        assert s.names == ["B"]
        assert s.events[0][0] == pytest.approx(1.0 + 1.5 + 0.3, abs=2 * DT)

    def test_click_on_current_display_is_fine(self):
        s = Script().look("A", 1).mouse().look("A", 1)
        assert s.names == [] and s.m.state == "idle"


class TestHysteresis:
    def test_needs_decisive_turn_to_leave(self):
        # Near the A|B bezel: B slightly closer, but not by the margin -> stay on A.
        s = Script(hysteresis=2.0).look("A", 1, {"A": 1, "B": 9})
        s.look("B", 2, {"A": 3.0, "B": 2.0})
        assert s.names == []
        s.look("B", 1, {"A": 5.0, "B": 2.0})  # beats A by 3 > 2
        assert s.names == ["B"]

    def test_bezel_noise_does_not_ping_pong(self):
        s = Script(hysteresis=2.0).look("A", 1)
        for i in range(60):  # 4 s of poses wobbling either side of the boundary
            near_b = i % 2 == 0
            d = {"A": 2.6, "B": 2.0} if near_b else {"A": 2.0, "B": 2.6}
            s.look("B" if near_b else "A", DT, d)
        assert s.names == []

    def test_entering_needs_no_margin_beyond_classification(self):
        # From "none" back onto a display that isn't current: plain classification counts.
        s = Script(start="A").look(NONE, 1).look("B", 1, {"A": 2.9, "B": 2.0})
        assert s.names == []  # still current=A: leaving A needs the margin
        s2 = Script(start=None).look("B", 1, {"A": 2.9, "B": 2.0})
        assert s2.names == [] and s2.m.current == "B"  # first sight adopted silently


class TestGapsAndStates:
    def test_single_stray_frame_does_not_reset_dwell(self):
        s = Script().look("A", 1).look("B", 0.2).look(NONE, DT).look("B", 0.15)
        assert s.names == ["B"]

    def test_long_gap_resets_dwell(self):
        s = Script().look("A", 1).look("B", 0.25).look(NONE, 0.5).look("B", 0.2)
        assert s.names == []

    def test_stray_frame_back_on_current_keeps_dwell(self):
        s = Script().look("A", 1).look("B", 0.2).look("A", DT).look("B", 0.15)
        assert s.names == ["B"]

    def test_none_keeps_current(self):
        s = Script().look("A", 1).look(NONE, 3).look("A", 1)
        assert s.names == [] and s.m.current == "A"
        assert Script().look(NONE, 1).m.state == "away"

    def test_no_face_disables_switching_and_keeps_current(self):
        s = Script().look("A", 1).look(None, 5)
        assert s.m.state == "no_face" and s.names == []
        s.look("A", 1)
        assert s.names == []
        s.look("B", 1)
        assert s.names == ["B"]

    def test_paused_never_switches(self):
        s = Script()
        s.paused = True
        s.look("B", 3)
        assert s.names == [] and s.m.state == "paused"
        s.paused = False
        s.look("B", 1)
        assert s.names == ["B"]

    def test_seed_without_event(self):
        m = DecisionMachine()
        m.seed("B")
        s = Script(machine=m, start="B").look("B", 2)
        assert s.names == []


class TestRealisticStream:
    def test_noisy_session(self):
        """Typing on A, glance at B (too short), phone, then a real switch to B, a click, back to A."""
        import random

        rng = random.Random(4)

        def noisy(d, secs):
            for _ in range(round(secs * FPS)):
                x = d if rng.random() > 0.06 else rng.choice(["A", "B", NONE])
                s.look(x, DT)

        s = Script()
        s.typed()
        noisy("A", 2)
        noisy("B", 0.25)  # glance
        noisy("A", 1)
        noisy(NONE, 2)  # phone
        noisy("A", 1)
        noisy("B", 1.5)  # real switch (typing was > 2 s ago)
        s.mouse()
        noisy("B", 1)
        noisy("A", 2.5)
        assert s.names == ["B", "A"]
