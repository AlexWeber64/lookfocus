"""Start at login: an XDG autostart entry (~/.config/autostart/lookfocus.desktop), per user, no root."""

from __future__ import annotations

import os
import re
from pathlib import Path

from lookfocus.paths import app_command

NAME = "lookfocus.desktop"


def path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / NAME


def quote(arg: str) -> str:
    """Desktop Entry Exec quoting (not shell quoting): double quotes, with " ` $ \\ escaped."""
    if arg and not re.search(r"[\s\"'\\><~|&;$*?#()`]", arg):
        return arg
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'


def command() -> str:
    return " ".join(quote(a) for a in app_command("tray"))


def entry(cmd: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=LookFocus\n"
        "Comment=Look at a monitor and just type\n"
        f"Exec={cmd}\n"
        "Icon=input-mouse\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def registered() -> str | None:
    try:
        text = path().read_text(encoding="utf-8")
    except OSError:
        return None
    return next((ln[5:] for ln in text.splitlines() if ln.startswith("Exec=")), "")


def is_enabled() -> bool:
    return registered() is not None


def set_enabled(enabled: bool) -> None:
    p = path()
    if enabled:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(entry(command()), encoding="utf-8")
    else:
        p.unlink(missing_ok=True)


def refresh() -> bool:
    """If autostart is on but points at another copy (venv moved), point it at this one."""
    current = registered()
    if current is None or current == command():
        return False
    set_enabled(True)
    return True
