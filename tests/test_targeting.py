import pytest

from lookfocus.displays import Monitor, Rect, build_displays
from lookfocus.targeting import VK_MENU, VK_UNASSIGNED, WindowInfo, activate, choose_target

TARGET, OTHER = 100, 200


class FakeApi:
    """Foreground becomes TARGET only when `works` (a stage name) is exercised."""

    def __init__(self, works: str | None, iconic: bool = False, fg: int = OTHER):
        self.works, self.iconic, self.fg = works, iconic, fg
        self.t = 0.0
        self.calls: list[str] = []
        self._pending_key = None

    def foreground(self):
        return self.fg

    def same_window(self, a, b):
        return a == b

    def is_iconic(self, hwnd):
        return self.iconic

    def restore(self, hwnd):
        self.calls.append("restore")
        self.iconic = False
        if self.works == "restore":
            self.fg = hwnd

    def set_foreground(self, hwnd):
        self.calls.append(f"set_foreground{'+' + self._pending_key if self._pending_key else ''}")
        stage = {None: "set_foreground", "vk": "input_unassigned_vk", "alt": "input_alt"}[self._pending_key]
        self._pending_key = None
        if self.works == stage:
            self.fg = hwnd
        return True  # lies, like the real API

    def attach_and_activate(self, hwnd):
        self.calls.append("attach")
        if self.works == "attach_thread_input":
            self.fg = hwnd
        return "fake"

    def tap_key(self, vk):
        self.calls.append(f"tap {vk:#x}")
        self._pending_key = {VK_UNASSIGNED: "vk", VK_MENU: "alt"}[vk]
        return True

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class TestLadder:
    @pytest.mark.parametrize(
        "stage", ["set_foreground", "attach_thread_input", "input_unassigned_vk", "input_alt"]
    )
    def test_stops_at_the_first_stage_that_works(self, stage):
        api = FakeApi(stage)
        act = activate(api, TARGET)
        assert act.ok and act.stage == stage
        assert [s.name for s in act.tried][-1] == stage
        assert all(not s.ok for s in act.tried[:-1]) and act.tried[-1].ok

    def test_order_of_attempts(self):
        api = FakeApi(None)
        act = activate(api, TARGET)
        assert not act.ok and act.stage is None and act.reason
        assert api.calls == [
            "set_foreground",
            "attach",
            f"tap {VK_UNASSIGNED:#x}",
            "set_foreground+vk",
            f"tap {VK_MENU:#x}",
            "set_foreground+alt",
        ]
        assert [s.name for s in act.tried] == [
            "set_foreground",
            "attach_thread_input",
            "input_unassigned_vk",
            "input_alt",
        ]

    def test_each_failed_stage_waits_its_timeout(self):
        act = activate(FakeApi(None), TARGET, stage_timeout=0.5)
        assert all(s.ms == pytest.approx(500, abs=20) for s in act.tried)

    def test_return_values_are_not_trusted(self):
        act = activate(FakeApi(None), TARGET)  # set_foreground always "returns True"
        assert not act.ok
        assert act.tried[0].detail == "returned True"

    def test_restore_only_if_minimized(self):
        api = FakeApi("set_foreground", iconic=False)
        activate(api, TARGET)
        assert "restore" not in api.calls
        api = FakeApi("set_foreground", iconic=True)
        act = activate(api, TARGET)
        assert api.calls[0] == "restore" and act.stage == "set_foreground"
        assert [s.name for s in act.tried] == ["restore", "set_foreground"]

    def test_restore_can_be_enough(self):
        act = activate(FakeApi("restore", iconic=True), TARGET)
        assert act.stage == "restore"

    def test_already_foreground(self):
        api = FakeApi(None, fg=TARGET)
        act = activate(api, TARGET)
        assert act.ok and act.stage == "already" and api.calls == []

    def test_alt_can_be_disabled(self):
        api = FakeApi("input_alt")
        act = activate(api, TARGET, allow_alt=False)
        assert not act.ok and f"tap {VK_MENU:#x}" not in api.calls

    def test_first_stage_skips_earlier_stages(self):
        api = FakeApi("attach_thread_input")
        act = activate(api, TARGET, first_stage="attach_thread_input")
        assert act.stage == "attach_thread_input" and api.calls == ["attach"]

    def test_fast_success_is_reported_quickly(self):
        act = activate(FakeApi("attach_thread_input"), TARGET)
        assert act.tried[-1].ms < 20


LAPTOP = Monitor(r"\\.\DISPLAY1", "p", "", Rect(0, 0, 2880, 1800), Rect(0, 0, 2880, 1704), True)
DS = build_displays([LAPTOP], zones=2)
LEFT, RIGHT = DS


def win(hwnd, rect, minimized=False):
    return WindowInfo(hwnd, f"w{hwnd}", "C", 1, "app", rect, minimized)


L1 = win(1, Rect(0, 0, 1440, 1700))
L2 = win(2, Rect(100, 100, 1200, 900))
R1 = win(3, Rect(1440, 0, 2880, 1700))
RMIN = win(4, Rect(1500, 100, 2800, 900), minimized=True)
STRADDLE = win(5, Rect(1000, 100, 2000, 900))  # centre 1500 -> right zone


class TestChooseTarget:
    def test_remembered_window_wins(self):
        assert choose_target(LEFT, 2, [L1, L2, R1], DS) is L2

    def test_falls_back_to_topmost_on_display(self):
        assert choose_target(LEFT, None, [R1, L2, L1], DS) is L2
        assert choose_target(RIGHT, None, [L1, R1], DS) is R1

    def test_remembered_window_that_moved_away_is_ignored(self):
        assert choose_target(LEFT, 3, [R1, L1], DS) is L1

    def test_closed_remembered_window_is_ignored(self):
        assert choose_target(LEFT, 99, [L1], DS) is L1

    def test_minimized_windows_are_never_targets(self):
        assert choose_target(RIGHT, 4, [RMIN], DS) is None
        assert choose_target(RIGHT, None, [RMIN, R1], DS) is R1

    def test_window_centre_decides_its_display(self):
        assert choose_target(RIGHT, None, [STRADDLE, R1], DS) is STRADDLE

    def test_nothing_eligible(self):
        assert choose_target(RIGHT, None, [L1, L2], DS) is None
