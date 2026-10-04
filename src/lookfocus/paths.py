"""Filesystem locations: the model ships with the code; user data lives in %APPDATA%\\LookFocus."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _code_root() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller one-folder build
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


ROOT = _code_root()
MODEL_PATH = ROOT / "models" / "face_landmarker.task"


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "LookFocus"


def app_command(*args: str, console: bool = False) -> list[str]:
    """Command line that starts this LookFocus again with `args` (autostart, elevated relaunch).

    Frozen build: LookFocus.exe (no console) or lookfocus-cli.exe next to it. From source: pythonw.exe /
    python.exe -m lookfocus, so the tray never opens a console window.
    """
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        sibling = exe.with_name("lookfocus-cli.exe" if console else "LookFocus.exe")
        return [str(sibling if sibling.exists() else exe), *args]
    variant = exe.with_name("python.exe" if console else "pythonw.exe")
    return [str(variant if variant.exists() else exe), "-m", "lookfocus", *args]
