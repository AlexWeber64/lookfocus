import json
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="tray app is Windows-only")


def _app(tmp_path, monkeypatch, answer=None):
    from lookfocus import config as K
    from lookfocus import tray

    app = object.__new__(tray.TrayApp)  # no icon, no threads: just the first-run logic
    app.cfg = K.Config()
    app.cfg_path = tmp_path / "config.json"
    app.state_path = tmp_path / "state.json"
    app._cfg_mtime = 0.0
    app.calibrating = False
    app.calls = []
    app.notify = lambda title, body: app.calls.append(("notify", title))
    app.start_calibration = lambda: app.calls.append("calibrate")
    app._run = lambda fn: fn()  # run dialogs inline
    monkeypatch.setattr(tray, "message_box", lambda text, flags=0: app.calls.append("dialog") or answer)
    return app


def test_first_run_on_one_monitor_offers_zones_then_calibrates(tmp_path, monkeypatch):
    from lookfocus.tray import IDYES

    app = _app(tmp_path, monkeypatch, answer=IDYES)
    app._welcome(one_display=True)
    assert app.calls == ["dialog", "calibrate"]
    assert app.cfg.zones == 2
    assert json.loads(app.cfg_path.read_text())["zones"] == 2  # saved, so the next start keeps it
    assert json.loads(app.state_path.read_text())["welcomed"] is True


def test_first_run_with_several_monitors_just_calibrates(tmp_path, monkeypatch):
    from lookfocus.tray import IDYES

    app = _app(tmp_path, monkeypatch, answer=IDYES)
    app._welcome(one_display=False)
    assert app.calls == ["dialog", "calibrate"] and app.cfg.zones == 0


def test_declining_the_welcome_changes_nothing_and_it_is_not_shown_again(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, answer=7)  # IDNO
    app._welcome(one_display=True)
    assert app.calls == ["dialog", ("notify", "Set up later")] and app.cfg.zones == 0
    app.calls.clear()
    app._welcome(one_display=True)  # next start: a notification, no dialog
    assert app.calls == [("notify", "LookFocus isn't set up yet")]


def test_changing_zones_recalibrates_and_same_value_is_a_no_op(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.set_zones(0)
    assert app.calls == []
    app.set_zones(3)
    assert app.calls == ["calibrate"] and json.loads(app.cfg_path.read_text())["zones"] == 3
    app.calibrating = True
    app.set_zones(2)  # not while a calibration is running
    assert app.cfg.zones == 3
