import pytest

from lookfocus.camera_sharing import WindowsBuild, feature_available, probe_command
from lookfocus.camera_usage import (
    NOTICE_BODY_MAX,
    NOTICE_TITLE_MAX,
    ActivityPolicy,
    busy_notice,
    friendly_app_name,
    yield_notice,
)

FIREFOX, TEAMS, ZOOM = 15156, 4242, 777


class TestActivityPolicy:
    def test_newcomer_while_holding_triggers_yield(self):
        p = ActivityPolicy()
        p.start_holding({})
        assert p.update(0.0, {}, holding=True, sharing_on=False) is None
        assert p.update(1.0, {TEAMS: False}, holding=True, sharing_on=False) == "yield"
        assert p.yielded and p.blockers == {TEAMS}

    def test_process_present_when_we_got_frames_is_ignored(self):
        # Firefox idly keeps the camera open (measured); LookFocus still gets frames, so it isn't blocked.
        p = ActivityPolicy()
        p.start_holding({FIREFOX: False})
        assert p.update(1.0, {FIREFOX: False}, True, False) is None
        assert p.update(2.0, {FIREFOX: False, TEAMS: False}, True, False) == "yield"
        assert p.blockers == {TEAMS}

    def test_no_yield_when_sharing_is_on(self):
        p = ActivityPolicy()
        p.start_holding({})
        assert p.update(1.0, {TEAMS: True}, True, sharing_on=True) is None
        assert not p.yielded
        # and it stays accepted even if the sharing state later reads off
        assert p.update(2.0, {TEAMS: True}, True, sharing_on=False) is None

    def test_no_yield_when_not_holding(self):
        p = ActivityPolicy()
        assert p.update(1.0, {TEAMS: False}, holding=False, sharing_on=False) is None

    def test_resume_after_blocker_streams_then_stops(self):
        p = ActivityPolicy(grace_s=2.0)
        p.start_holding({})
        p.update(0.0, {TEAMS: False}, True, False)  # yield
        assert p.update(1.0, {TEAMS: True}, False, False) is None  # Teams now has video
        assert p.update(60.0, {TEAMS: True}, False, False) is None  # long call, no timeout while streaming
        assert p.update(61.0, {TEAMS: False}, False, False) is None  # call ended, device still open
        assert p.update(62.9, {TEAMS: False}, False, False) is None
        assert p.update(63.0, {TEAMS: False}, False, False) == "resume"
        assert TEAMS in p.ignored  # an idle open device shouldn't re-trigger a yield
        assert p.update(64.0, {TEAMS: False}, True, False) is None

    def test_resume_when_blocker_closes(self):
        p = ActivityPolicy(grace_s=1.0)
        p.start_holding({})
        p.update(0.0, {ZOOM: False}, True, False)
        p.update(0.5, {ZOOM: True}, False, False)
        assert p.update(5.0, {}, False, False) is None
        assert p.update(6.0, {}, False, False) == "resume"

    def test_idle_holder_times_out(self):
        p = ActivityPolicy(grace_s=2.0, idle_timeout_s=10.0)
        p.start_holding({})
        p.update(0.0, {FIREFOX: False}, True, False)  # yield
        assert p.update(9.9, {FIREFOX: False}, False, False) is None  # still might be starting
        assert p.update(10.0, {FIREFOX: False}, False, False) is None  # grace starts
        assert p.update(12.0, {FIREFOX: False}, False, False) == "resume"
        assert p.update(13.0, {FIREFOX: False}, True, False) is None  # ignored from now on

    def test_ignored_process_that_closes_and_reopens_triggers_again(self):
        p = ActivityPolicy(grace_s=0.0, idle_timeout_s=0.0)
        p.start_holding({})
        p.update(0.0, {FIREFOX: False}, True, False)
        assert p.update(1.0, {FIREFOX: False}, False, False) == "resume"
        p.update(2.0, {}, True, False)  # Firefox closed the camera
        assert p.update(3.0, {FIREFOX: False}, True, False) == "yield"

    def test_late_joiner_gets_its_own_start_window(self):
        p = ActivityPolicy(grace_s=0.0, idle_timeout_s=10.0)
        p.start_holding({})
        p.update(0.0, {TEAMS: False}, True, False)
        p.update(1.0, {TEAMS: True}, False, False)
        p.update(8.0, {TEAMS: False, ZOOM: False}, False, False)  # Teams done, Zoom arrives
        assert p.update(9.0, {TEAMS: False, ZOOM: False}, False, False) is None  # Zoom still starting
        assert p.update(18.1, {TEAMS: False, ZOOM: False}, False, False) == "resume"

    def test_busy_at_start_waits_for_streaming_apps(self):
        p = ActivityPolicy(grace_s=1.0)
        assert not p.block_on(0.0, {FIREFOX: False})  # idle holder isn't why we got no frames
        assert p.block_on(0.0, {FIREFOX: True})
        assert p.update(5.0, {FIREFOX: True}, False, False) is None
        p.update(6.0, {FIREFOX: False}, False, False)
        assert p.update(7.0, {FIREFOX: False}, False, False) == "resume"

    def test_busy_ignores_idle_holders(self):
        # Measured: Firefox idles with the camera open, another app streams, LookFocus gets no frames.
        p = ActivityPolicy(grace_s=2.0, idle_timeout_s=10.0)
        assert p.block_on(11.5, {FIREFOX: False, ZOOM: True})
        assert p.update(15.0, {FIREFOX: False, ZOOM: True}, False, False) is None
        assert p.update(15.8, {FIREFOX: False}, False, False) is None  # Zoom exits
        assert p.update(17.8, {FIREFOX: False}, False, False) == "resume"  # grace only, no idle timeout

    def test_manual_resume_waives_current_blockers(self):
        p = ActivityPolicy()
        p.start_holding({})
        p.update(0.0, {TEAMS: False}, True, False)
        p.user_resume({TEAMS: False})
        assert not p.yielded
        assert p.update(1.0, {TEAMS: False}, True, False) is None

    def test_baseline_forgets_processes_that_close(self):
        p = ActivityPolicy()
        p.start_holding({FIREFOX: False})
        p.update(1.0, {}, True, False)
        assert p.update(2.0, {FIREFOX: False}, True, False) == "yield"


class TestNames:
    @pytest.mark.parametrize(
        ("exe", "name"),
        [
            ("firefox", "Firefox"),
            ("ms-teams.exe", "Microsoft Teams"),
            ("Zoom", "Zoom"),
            (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "Google Chrome"),
            ("WindowsCamera", "Camera"),
            ("SomeTool", "SomeTool"),
            ("", "Another app"),
        ],
    )
    def test_friendly_app_name(self, exe, name):
        assert friendly_app_name(exe) == name


class TestNotices:
    def test_yield_notice_mentions_setting_when_available(self):
        title, body = yield_notice(["Microsoft Teams"], True, "Integrated Camera")  # 255-char budget
        assert title == "Camera released for Microsoft Teams"
        assert "Allow multiple apps to use camera at the same time" in body
        assert "Settings > Bluetooth & devices > Cameras" in body

    def test_yield_notice_without_setting(self):
        _, body = yield_notice(["Zoom"], False)
        assert "Allow multiple apps" not in body and "will resume afterwards" in body

    def test_duplicate_app_names_are_merged(self):
        title, _ = yield_notice(["Firefox", "Firefox"], False)
        assert title == "Camera released for Firefox"

    @pytest.mark.parametrize("n", [1, 2, 5])
    def test_notices_fit_tray_balloon_limits(self, n):
        apps = [f"Some Very Long Application Name {i}" for i in range(n)]
        notices = (yield_notice(apps, True, "A Long Camera Device Name Here"), busy_notice(apps, True))
        for title, body in notices:
            assert len(title) <= NOTICE_TITLE_MAX
            assert len(body) <= NOTICE_BODY_MAX

    def test_busy_notice(self):
        title, body = busy_notice(["Firefox"], False)
        assert title == "Camera in use by Firefox" and "waiting" in body


class TestSharingPure:
    @pytest.mark.parametrize(
        ("build", "ubr", "ok"),
        [
            (22631, 9999, False),
            (26100, 3320, False),
            (26100, 3321, True),
            (26200, 9457, True),
            (28000, 1, True),
        ],
    )
    def test_feature_available(self, build, ubr, ok):
        assert feature_available(WindowsBuild(build, ubr, "")) is ok

    def test_probe_command_runs_module(self):
        cmd = probe_command("msmf", 0, 2.0)
        assert cmd[1:3] == ["-m", "lookfocus.camera_sharing"] and "--probe-child" in cmd
