"""Start with Windows: the HKCU Run key (per user, no admin rights, no installer)."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "LookFocus"


def command() -> str:
    """What Windows runs at sign-in: the console-less interpreter (or the frozen exe) in tray mode."""
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline([sys.executable])
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return subprocess.list2cmdline([str(exe), "-m", "lookfocus", "tray"])


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VALUE)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(k, VALUE)
