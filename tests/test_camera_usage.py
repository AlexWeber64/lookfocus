import pytest

from lookfocus.camera_sharing import WindowsBuild, feature_available, probe_command
from lookfocus.camera_usage import (
    NOTICE_BODY_MAX,
    NOTICE_TITLE_MAX,
    CameraUse,
    YieldPolicy,
    app_display_name,
    busy_notice,
    exe_app_id,
    in_use_now,
    started_since,
    yield_notice,
)

ME = exe_app_id(r"C:\Users\me\AppData\Local\Programs\Python\Python312\python.exe")
TEAMS = "MSTeams_8wekyb3d8bbwe"
ZOOM = exe_app_id(r"C:\Users\me\AppData\Roaming\Zoom\bin\Zoom.exe")


def use(app, start, stop=0):
    return CameraUse(app, start, stop)


def snap(*uses):
    return {u.app_id: u for u in uses}


class TestNames:
    @pytest.mark.parametrize(
        ("app_id", "name"),
        [
            (ZOOM, "Zoom"),
            (TEAMS, "Microsoft Teams"),
            ("Microsoft.WindowsCamera_8wekyb3d8bbwe", "Camera"),
            ("windows.immersivecontrolpanel_cw5n1h2txyewy", "Settings"),
            ("Mozilla.Firefox_n80bbvh6b1yt2", "Mozilla.Firefox"),
            (exe_app_id(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"), "brave"),
        ],
    )
    def test_display_name(self, app_id, name):
        assert app_display_name(app_id) == name

    def test_exe_app_id_matches_registry_form(self):
        assert exe_app_id(r"C:\a\b.exe") == "NonPackaged\\C:#a#b.exe"


class TestDiff:
    def test_new_session_detected(self):
        prev = snap(use(TEAMS, 100, 200))
        cur = snap(use(TEAMS, 300))
        assert [u.app_id for u in started_since(prev, cur, set())] == [TEAMS]

    def test_new_entry_detected(self):
        assert [u.app_id for u in started_since({}, snap(use(ZOOM, 5)), set())] == [ZOOM]

    def test_ongoing_session_not_reported_twice(self):
        s = snap(use(TEAMS, 300))
        assert started_since(s, s, set()) == []

    def test_stopped_sessions_ignored(self):
        assert started_since({}, snap(use(TEAMS, 300, 400)), set()) == []

    def test_own_app_excluded_case_insensitively(self):
        assert started_since({}, snap(use(ME, 5)), {ME.upper()}) == []

    def test_in_use_now(self):
        cur = snap(use(TEAMS, 1), use(ZOOM, 1, 2), use(ME, 1))
        assert in_use_now(cur, {ME}) == {TEAMS}


class TestYieldPolicy:
    def test_yields_when_sharing_off(self):
        p = YieldPolicy()
        cur = snap(use(TEAMS, 10))
        assert p.on_usage(0.0, [cur[TEAMS]], cur, sharing_on=False) == "yield"
        assert p.yielded and p.blockers == {TEAMS}

    def test_keeps_camera_when_sharing_on(self):
        p = YieldPolicy()
        cur = snap(use(TEAMS, 10))
        assert p.on_usage(0.0, [cur[TEAMS]], cur, sharing_on=True) is None
        assert not p.yielded

    def test_resumes_after_grace_once_blocker_stops(self):
        p = YieldPolicy(grace_s=2.0)
        cur = snap(use(TEAMS, 10))
        p.on_usage(0.0, [cur[TEAMS]], cur, False)
        assert p.on_usage(5.0, [], cur, False) is None  # still in use
        done = snap(use(TEAMS, 10, 20))
        assert p.on_usage(6.0, [], done, False) is None  # grace starts
        assert p.on_usage(7.9, [], done, False) is None
        assert p.on_usage(8.0, [], done, False) == "resume"
        assert not p.yielded

    def test_grace_restarts_if_blocker_comes_back(self):
        p = YieldPolicy(grace_s=2.0)
        cur = snap(use(TEAMS, 10))
        p.on_usage(0.0, [cur[TEAMS]], cur, False)
        p.on_usage(1.0, [], snap(use(TEAMS, 10, 20)), False)
        again = snap(use(TEAMS, 30))
        assert p.on_usage(2.5, [again[TEAMS]], again, False) is None
        assert p.on_usage(10.0, [], snap(use(TEAMS, 30, 40)), False) is None  # new grace window
        assert p.on_usage(12.0, [], snap(use(TEAMS, 30, 40)), False) == "resume"

    def test_waits_for_every_app_that_started_while_yielded(self):
        p = YieldPolicy(grace_s=0.0)
        cur = snap(use(TEAMS, 10))
        p.on_usage(0.0, [cur[TEAMS]], cur, False)
        both = snap(use(TEAMS, 10), use(ZOOM, 11))
        assert p.on_usage(1.0, [both[ZOOM]], both, False) is None
        assert p.on_usage(2.0, [], snap(use(TEAMS, 10, 12), use(ZOOM, 11)), False) is None  # Zoom still on
        assert p.on_usage(3.0, [], snap(use(TEAMS, 10, 12), use(ZOOM, 11, 13)), False) == "resume"

    def test_manual_resume_ignores_current_session_only(self):
        p = YieldPolicy()
        cur = snap(use(TEAMS, 10))
        p.on_usage(0.0, [cur[TEAMS]], cur, False)
        p.user_resume(cur)
        assert not p.yielded
        # the same Teams session is reported again (e.g. a rescan): no new yield
        assert p.on_usage(1.0, [cur[TEAMS]], cur, False) is None
        # a later Teams session does yield again
        later = snap(use(TEAMS, 99))
        assert p.on_usage(9.0, [later[TEAMS]], later, False) == "yield"

    def test_block_on_when_camera_busy_at_start(self):
        p = YieldPolicy(grace_s=0.0)
        assert p.block_on({ZOOM})
        assert p.yielded and not p.block_on({TEAMS})
        assert p.on_usage(1.0, [], snap(use(ZOOM, 1, 2)), False) == "resume"

    def test_block_on_nothing_is_a_no_op(self):
        assert not YieldPolicy().block_on(set())


class TestNotices:
    def test_yield_notice_mentions_setting_when_available(self):
        title, body = yield_notice(["Microsoft Teams"], True, "Integrated Camera")
        assert title == "Camera released for Microsoft Teams"
        assert "Allow multiple apps to use camera at the same time" in body
        assert "Microsoft Teams started using the camera" in body

    def test_yield_notice_without_setting(self):
        _, body = yield_notice(["Zoom"], False)
        assert "Allow multiple apps" not in body and "resumes when Zoom is done" in body

    @pytest.mark.parametrize("n", [1, 2, 5])
    def test_notices_fit_tray_balloon_limits(self, n):
        apps = [f"Some Very Long Application Name {i}" for i in range(n)]
        notices = (yield_notice(apps, True, "A Long Camera Device Name Here"), busy_notice(apps, True))
        for title, body in notices:
            assert len(title) <= NOTICE_TITLE_MAX
            assert len(body) <= NOTICE_BODY_MAX

    def test_busy_notice(self):
        title, body = busy_notice(["Zoom"], False)
        assert title == "Camera in use by Zoom" and "waiting" in body


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
