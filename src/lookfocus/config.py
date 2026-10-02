"""User configuration: %APPDATA%\\LookFocus\\config.json.

Pure logic (no Win32): defaults, per-field validation, load/save, hotkey parsing. A bad value never stops
LookFocus: it falls back to the default and the problem is reported (tray notification + log). Unknown keys
are reported too (usually a typo).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
BACKENDS = ("msmf", "dshow")
POSE_SOURCES = ("matrix", "lm3d", "lm2d")


@dataclass(frozen=True)
class Config:
    # switching (see DECISIONS.md D-032)
    dwell_ms: int = 300
    typing_dwell_ms: int = 1000
    typing_grace_ms: int = 2000
    mouse_grace_ms: int = 1500
    hysteresis: float = 2.0
    none_margin: float = 6.0
    move_cursor_on_switch: bool = False
    allow_alt_fallback: bool = True
    # camera
    fps: float = 15.0
    camera_backend: str = "msmf"
    camera_index: int | None = None  # None = automatic (or by path/name below)
    camera_name: str = ""
    camera_path: str = ""
    yield_camera_to_other_apps: bool = True
    # displays
    zones: int = 0  # 0 = one logical display per monitor; N >= 2 splits the primary monitor
    pose_source: str = "matrix"
    # app
    hotkey: str = "ctrl+alt+shift+g"
    notifications: bool = True
    log_level: str = "INFO"


# field -> (validator returning an error message or None)
def _range(lo, hi):
    return lambda v: None if lo <= v <= hi else f"must be between {lo} and {hi}"


_RULES = {
    "dwell_ms": _range(0, 5000),
    "typing_dwell_ms": _range(0, 10000),
    "typing_grace_ms": _range(0, 30000),
    "mouse_grace_ms": _range(0, 30000),
    "hysteresis": _range(0, 50),
    "none_margin": _range(0.5, 100),
    "fps": _range(1, 30),
    "camera_backend": lambda v: None if v in BACKENDS else f"must be one of {', '.join(BACKENDS)}",
    "camera_index": lambda v: None if v is None or v >= 0 else "must be 0 or more, or null for automatic",
    "zones": lambda v: None if v == 0 or 2 <= v <= 8 else "must be 0 (off) or 2 to 8",
    "pose_source": lambda v: None if v in POSE_SOURCES else f"must be one of {', '.join(POSE_SOURCES)}",
    "hotkey": lambda v: _hotkey_error(v),
    "log_level": lambda v: None if v.upper() in LOG_LEVELS else f"must be one of {', '.join(LOG_LEVELS)}",
}


def _type_ok(value, default) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, int | float) and not isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    return True


def from_dict(data: dict) -> tuple[Config, list[str]]:
    """Validated Config plus human-readable problems (each bad field falls back to its default)."""
    problems: list[str] = []
    defaults = Config()
    values = {}
    known = {f.name for f in fields(Config)}
    for key in data:
        if key not in known:
            problems.append(f"unknown setting {key!r} ignored")
    for f in fields(Config):
        if f.name not in data:
            continue
        v, d = data[f.name], getattr(defaults, f.name)
        if f.name == "camera_index":
            ok_type = v is None or (isinstance(v, int) and not isinstance(v, bool))
        else:
            ok_type = _type_ok(v, d)
        if not ok_type:
            problems.append(f"{f.name}: {v!r} has the wrong type; using {d!r}")
            continue
        if isinstance(d, float) and isinstance(v, int):
            v = float(v)
        rule = _RULES.get(f.name)
        err = rule(v) if rule else None
        if err:
            problems.append(f"{f.name}: {v!r} {err}; using {d!r}")
            continue
        values[f.name] = v.upper() if f.name == "log_level" else v
    return Config(**values), problems


def to_dict(cfg: Config) -> dict:
    return asdict(cfg)


def load(path: Path) -> tuple[Config, list[str]]:
    """Missing file -> defaults (and the caller writes them out). Unreadable JSON -> defaults + problem."""
    if not path.exists():
        return Config(), []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Config(), [f"couldn't read {path.name} ({e}); using defaults"]
    if not isinstance(data, dict):
        return Config(), [f"{path.name} must contain a JSON object; using defaults"]
    return from_dict(data)


def save(cfg: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(to_dict(cfg), f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------------------------------------
# hotkeys ("ctrl+alt+shift+g") -> RegisterHotKey modifiers + virtual key
# ------------------------------------------------------------------------------------------------

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT, "win": MOD_WIN}
_NAMED_KEYS = {
    "space": 0x20,
    "pause": 0x13,
    "scrolllock": 0x91,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "enter": 0x0D,
    "backspace": 0x08,
}


def parse_hotkey(text: str) -> tuple[int, int]:
    """(modifiers incl. MOD_NOREPEAT, virtual key). Raises ValueError with a readable message."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *mods, key = parts
    m = MOD_NOREPEAT
    for name in mods:
        if name not in _MODS:
            raise ValueError(f"unknown modifier {name!r} (use ctrl, alt, shift, win)")
        m |= _MODS[name]
    if m == MOD_NOREPEAT:
        raise ValueError("a global hotkey needs at least one modifier (ctrl, alt, shift or win)")
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    else:
        raise ValueError(f"unknown key {key!r}")
    return m, vk


def _hotkey_error(text: str) -> str | None:
    try:
        parse_hotkey(text)
        return None
    except ValueError as e:
        return f"is not a valid hotkey ({e})"
