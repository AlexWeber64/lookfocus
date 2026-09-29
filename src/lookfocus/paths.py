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
