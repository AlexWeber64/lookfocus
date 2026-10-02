import json

import pytest

from lookfocus import config as K


def test_defaults_match_spec():
    c = K.Config()
    assert (c.dwell_ms, c.mouse_grace_ms, c.typing_dwell_ms, c.fps) == (300, 1500, 1000, 15.0)
    assert c.hotkey == "ctrl+alt+shift+g" and c.move_cursor_on_switch is False


def test_round_trip(tmp_path):
    p = tmp_path / "config.json"
    cfg = K.Config(dwell_ms=450, zones=2, camera_name="Integrated Camera", camera_index=0)
    K.save(cfg, p)
    back, problems = K.load(p)
    assert back == cfg and problems == []
    assert json.loads(p.read_text())["dwell_ms"] == 450


def test_missing_file_is_defaults(tmp_path):
    assert K.load(tmp_path / "nope.json") == (K.Config(), [])


def test_bad_values_fall_back_individually():
    cfg, problems = K.from_dict({"dwell_ms": -5, "fps": 120, "hysteresis": 3, "camera_backend": "v4l2"})
    assert cfg.dwell_ms == 300 and cfg.fps == 15.0 and cfg.camera_backend == "msmf"
    assert cfg.hysteresis == 3.0  # int accepted for a float field
    assert len(problems) == 3 and any("dwell_ms" in p for p in problems)


def test_wrong_types():
    cfg, problems = K.from_dict({"dwell_ms": "300", "move_cursor_on_switch": 1, "zones": True})
    assert cfg == K.Config()
    assert len(problems) == 3


def test_unknown_keys_reported():
    cfg, problems = K.from_dict({"dwel_ms": 200})
    assert cfg == K.Config() and problems == ["unknown setting 'dwel_ms' ignored"]


def test_camera_index_null_or_nonnegative():
    assert K.from_dict({"camera_index": None})[0].camera_index is None
    assert K.from_dict({"camera_index": 1})[0].camera_index == 1
    assert K.from_dict({"camera_index": -1})[1]


def test_zones_and_log_level():
    assert K.from_dict({"zones": 1})[1] and K.from_dict({"zones": 3})[0].zones == 3
    assert K.from_dict({"log_level": "debug"})[0].log_level == "DEBUG"


def test_unreadable_json(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ not json")
    cfg, problems = K.load(p)
    assert cfg == K.Config() and "using defaults" in problems[0]
    p.write_text("[1, 2]")
    assert "JSON object" in K.load(p)[1][0]


class TestHotkey:
    def test_default(self):
        mods, vk = K.parse_hotkey("ctrl+alt+shift+g")
        assert mods == K.MOD_CONTROL | K.MOD_ALT | K.MOD_SHIFT | K.MOD_NOREPEAT and vk == ord("G")

    @pytest.mark.parametrize(("text", "vk"), [("win+f9", 0x78), ("Ctrl + Pause", 0x13), ("alt+1", ord("1"))])
    def test_keys(self, text, vk):
        assert K.parse_hotkey(text)[1] == vk

    @pytest.mark.parametrize("text", ["g", "ctrl+", "hyper+g", "ctrl+f25", "ctrl+banana", ""])
    def test_invalid(self, text):
        with pytest.raises(ValueError):
            K.parse_hotkey(text)

    def test_invalid_hotkey_falls_back(self):
        cfg, problems = K.from_dict({"hotkey": "g"})
        assert cfg.hotkey == "ctrl+alt+shift+g" and "modifier" in problems[0]
