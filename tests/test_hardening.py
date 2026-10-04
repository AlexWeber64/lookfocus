import logging
import sys
import threading
from collections import deque
from pathlib import Path

import pytest

from lookfocus import paths

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="tray app is Windows-only")


def test_app_command_from_source(monkeypatch, tmp_path):
    for name in ("python.exe", "pythonw.exe"):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert paths.app_command("tray") == [str(tmp_path / "pythonw.exe"), "-m", "lookfocus", "tray"]
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))  # the tray asking for a console
    assert paths.app_command("run", console=True)[0] == str(tmp_path / "python.exe")


def test_app_command_frozen(monkeypatch, tmp_path):
    for name in ("LookFocus.exe", "lookfocus-cli.exe"):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "lookfocus-cli.exe"))
    assert paths.app_command("tray") == [str(tmp_path / "LookFocus.exe"), "tray"]
    assert paths.app_command("run", "--stats", console=True) == [
        str(tmp_path / "lookfocus-cli.exe"),
        "run",
        "--stats",
    ]


class _FakeSlot:
    def get(self):
        return 0, None


class _FakeDaemon:
    poses = _FakeSlot()

    def __init__(self, dead):
        self.dead = dead

    def dead_threads(self):
        return self.dead


def _app(calls, dead):
    from lookfocus.tray import TrayApp

    app = object.__new__(TrayApp)  # no icon, no threads: just the watchdog logic
    app._restarts = deque()
    app.log_path = Path("lookfocus.log")
    app.daemon = _FakeDaemon(dead)
    app._stop_engine = lambda: calls.append("stop")
    app._start_engine = lambda: calls.append("start")
    app._set_state = lambda state, detail="": calls.append(("state", state))
    app.notify = lambda title, body: calls.append(("notify", title))
    return app


def test_watchdog_ignores_a_healthy_engine():
    calls = []
    _app(calls, [])._check_engine()
    assert calls == []


def test_watchdog_restarts_then_gives_up():
    from lookfocus.tray import RESTART_BUDGET

    calls = []
    app = _app(calls, ["capture"])
    for _ in range(RESTART_BUDGET + 1):
        app._check_engine()
    assert calls.count("start") == RESTART_BUDGET
    assert calls[-2:] == [("state", "error"), ("notify", "LookFocus stopped")]


def test_uncaught_thread_exception_is_logged(monkeypatch, caplog):
    from lookfocus.tray import install_crash_logging

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # restored after the test
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    install_crash_logging()

    def boom():
        raise ValueError("boom")

    with caplog.at_level(logging.CRITICAL, logger="lookfocus"):
        t = threading.Thread(target=boom, name="worker")
        t.start()
        t.join()
    assert any("worker" in r.getMessage() and r.exc_info[0] is ValueError for r in caplog.records)
