"""Platform backends: the Win32 modules on Windows, lookfocus.linux on Linux (KDE Plasma on Wayland).

Each accessor imports its module on first call, so importing this file (or anything that uses it) loads no
platform code, and the pure modules stay importable everywhere.
"""

from __future__ import annotations

import sys
from types import ModuleType

WINDOWS = sys.platform == "win32"
AUTOSTART_LABEL = "Start with Windows" if WINDOWS else "Start at login"


def focus() -> ModuleType:
    """Window list, foreground tracker, activation, pointer (windows_focus / linux.focus)."""
    if WINDOWS:
        from lookfocus import windows_focus as m
    else:
        from lookfocus.linux import focus as m
    return m


def monitors() -> ModuleType:
    if WINDOWS:
        from lookfocus import monitors as m
    else:
        from lookfocus.linux import monitors as m
    return m


def overlay() -> ModuleType:
    if WINDOWS:
        from lookfocus import overlay as m
    else:
        from lookfocus.linux import overlay as m
    return m


def sysevents() -> ModuleType:
    if WINDOWS:
        from lookfocus import sysevents as m
    else:
        from lookfocus.linux import sysevents as m
    return m


def autostart() -> ModuleType:
    if WINDOWS:
        from lookfocus import autostart as m
    else:
        from lookfocus.linux import autostart as m
    return m


def input_poller(clock):
    if WINDOWS:
        from lookfocus.inputactivity import InputPoller
    else:
        from lookfocus.linux.input import LinuxInputPoller as InputPoller
    return InputPoller(clock=clock)
