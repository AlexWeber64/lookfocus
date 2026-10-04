"""Start with Windows: the HKCU Run key (per user, no admin rights, no installer)."""

from __future__ import annotations

import contextlib
import subprocess
import winreg

from lookfocus.paths import app_command

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "LookFocus"


def command() -> str:
    """What Windows runs at sign-in: the console-less interpreter (or the frozen exe) in tray mode."""
    return subprocess.list2cmdline(app_command("tray"))


def registered() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            value, _type = winreg.QueryValueEx(k, VALUE)
            return str(value)
    except OSError:
        return None


def is_enabled() -> bool:
    return registered() is not None


def set_enabled(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(k, VALUE)


def refresh() -> bool:
    """If autostart is on but points at another copy (venv moved, switched to the frozen build), point it
    at this one, so sign-in never starts a stale or missing program. Returns True if it changed."""
    current = registered()
    if current is None or current == command():
        return False
    set_enabled(True)
    return True
