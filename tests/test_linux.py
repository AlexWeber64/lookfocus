"""The Linux backend's pure logic: KWin window filtering, screens, PipeWire camera discovery, the bridge
script, autostart quoting, KDE shortcuts, the Wayland wire format. Hardware-free; runs everywhere except
where noted."""

import json
import subprocess
import sys

import pytest

from lookfocus import config as K
from lookfocus.displays import Rect, build_displays
from lookfocus.inputactivity import InputTracker
from lookfocus.linux import windowlist as W

ME = 4242


def win(**kw) -> dict:
    d = {
        "id": "{a}",
        "root": "{a}",
        "caption": "Editor",
        "cls": "org.kde.kate",
        "app": "kate",
        "pid": 100,
        "x": 0,
        "y": 0,
        "w": 800,
        "h": 600,
        "minimized": False,
        "normal": True,
        "dialog": False,
        "modal": False,
        "transient": False,
        "special": False,
        "desktopWindow": False,
        "dock": False,
        "popup": False,
        "skipSwitcher": False,
        "managed": True,
        "deleted": False,
        "hidden": False,
        "onCurrentDesktop": True,
    }
    d.update(kw)
    return d


class TestWindowList:
    def test_normal_window_is_a_candidate(self):
        assert W.is_candidate(win(), own_pid=ME)

    @pytest.mark.parametrize(
        "change",
        [
            {"pid": ME},  # LookFocus's own overlay / debug view
            {"desktopWindow": True, "normal": False},  # plasmashell desktop
            {"dock": True, "normal": False},  # panels
            {"special": True},
            {"skipSwitcher": True},  # e.g. xwaylandvideobridge
            {"transient": True, "root": "{parent}"},  # reached through its main window
            {"onCurrentDesktop": False},
            {"managed": False},
            {"deleted": True},
            {"normal": False, "dialog": False},
            {"w": 0},
        ],
    )
    def test_excluded(self, change):
        assert not W.is_candidate(win(**change), own_pid=ME)

    def test_minimized_windows_stay_candidates_and_targeting_skips_them(self):
        assert W.is_candidate(win(minimized=True, hidden=True), own_pid=ME)

    def test_candidates_are_topmost_first(self):
        snap = {"windows": [win(id="{bottom}", root="{bottom}"), win(id="{top}", root="{top}")]}
        assert [w.hwnd for w in W.candidate_windows(snap, own_pid=ME)] == ["{top}", "{bottom}"]

    def test_window_info_rounds_logical_geometry(self):
        info = W.window_info(win(x=502.29, y=334.29, w=640, h=359.99))
        assert info.rect == Rect(502, 334, 1142, 694)
        assert info.exe == "kate" and info.elevated is None

    def test_root_of_a_dialog(self):
        snap = {"windows": [win(id="{main}", root="{main}"), win(id="{dlg}", root="{main}", transient=True)]}
        assert W.root_id(snap, "{dlg}") == "{main}"
        assert W.root_id(snap, "{gone}") == "{gone}"
        assert W.root_id(snap, None) is None


class TestMonitors:
    def test_kwin_screens_to_monitors(self):
        from lookfocus.linux.monitors import monitors_from

        info = {
            "order": ["DP-1", "eDP-1"],
            "screens": [
                {
                    "name": "eDP-1",
                    "x": 0,
                    "y": 0,
                    "w": 1646,
                    "h": 1029,
                    "scale": 1.75,
                    "manufacturer": "Samsung",
                },
                {
                    "name": "DP-1",
                    "x": 1646,
                    "y": 0,
                    "w": 2560,
                    "h": 1440,
                    "scale": 1.0,
                    "model": "DELL U2720Q",
                },
            ],
        }
        mons = monitors_from(info)
        laptop, dell = mons
        assert laptop.name == "" and laptop.dpi == 168 and not laptop.primary
        assert dell.name == "DELL U2720Q" and dell.primary and dell.rect == Rect(1646, 0, 4206, 1440)
        ds = build_displays(mons, 0)
        assert [d.short for d in ds] == ["Monitor 1", "Monitor 2"]
        assert ds[1].label == "Monitor 2 (DELL U2720Q, primary)"

    def test_single_built_in_panel(self):
        from lookfocus.linux.monitors import monitors_from

        mons = monitors_from({"order": [], "screens": [{"name": "eDP-1", "w": 1646, "h": 1029}]})
        assert mons[0].primary
        assert build_displays(mons, 2)[0].label.endswith("(left half of Monitor 1, built-in display)")


class TestPipeWire:
    def test_camera_nodes_only(self):
        from lookfocus.linux.camera import pipewire_cameras_from

        dump = [
            {
                "id": 120,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {
                        "media.class": "Video/Source",
                        "media.role": "Camera",
                        "device.api": "libcamera",
                        "node.name": "libcamera_input.__SB_.LNK0",
                        "node.description": "Built-in Front Camera",
                        "object.serial": 120,
                        "api.libcamera.rotation": 180,
                    }
                },
            },
            {  # a screen cast is a Video/Source too
                "id": 130,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"media.class": "Video/Source", "node.name": "xdg-desktop-portal-kde"}},
            },
            {
                "id": 5,
                "type": "PipeWire:Interface:Device",
                "info": {"props": {"media.class": "Video/Source"}},
            },
        ]
        (cam,) = pipewire_cameras_from(dump)
        assert cam.node_name == "libcamera_input.__SB_.LNK0" and cam.rotation == 180
        assert cam.description == "Built-in Front Camera"


class TestBridgeScript:
    def test_placeholders_filled_and_strings_escaped(self):
        from lookfocus.linux.kwin import render_script

        js = render_script(':1.99"x', "Ctrl+Alt+Shift+G")
        assert "@" not in js.replace("@BUS@", "")  # every placeholder replaced
        assert 'var BUS = ":1.99\\"x"' in js
        assert 'HOTKEY = "Ctrl+Alt+Shift+G"' in js

    def test_no_hotkey_means_no_shortcut(self):
        from lookfocus.linux.kwin import render_script

        assert 'HOTKEY = ""' in render_script(":1.5")


class TestAutostart:
    def test_desktop_entry_quoting(self):
        from lookfocus.linux.autostart import entry, quote

        assert quote("/usr/bin/python3") == "/usr/bin/python3"
        assert quote("/home/a b/py") == '"/home/a b/py"'
        assert quote('a"$`\\') == '"a\\"\\$\\`\\\\"'
        assert "Exec=/x -m lookfocus tray\n" in entry("/x -m lookfocus tray")


class TestShortcut:
    @pytest.mark.parametrize(
        ("text", "kde"),
        [
            ("ctrl+alt+shift+g", "Ctrl+Alt+Shift+G"),
            ("shift+ctrl+p", "Ctrl+Shift+P"),
            ("win+pageup", "Meta+PgUp"),
            ("alt+f9", "Alt+F9"),
            ("ctrl+space", "Ctrl+Space"),
        ],
    )
    def test_kde_key_sequence(self, text, kde):
        assert K.kde_key_sequence(text) == kde

    def test_invalid_hotkey_raises(self):
        with pytest.raises(ValueError):
            K.kde_key_sequence("g")


class TestWaylandWire:
    def test_string_roundtrip_and_padding(self):
        from lookfocus.linux.wayland import _read_string, _string

        for s in ("", "wl_seat", "ext_idle_notifier_v1"):
            raw = _string(s)
            assert len(raw) % 4 == 0
            assert _read_string(raw + b"tail", 0) == (s, len(raw))


def test_input_slack_is_configurable():
    t = InputTracker(slack_s=0.15)
    t.update(0.0, 10.0, (0, 0), False)  # baseline
    t.update(1.0, 0.0, (50, 0), False)  # pointer moved, input now
    st = t.update(1.12, 0.0, (50, 0), False)  # input 120 ms after the last seen move: still the mouse
    assert st.keyboard_at == float("-inf")
    st = t.update(1.5, 0.0, (50, 0), False)
    assert st.keyboard_at == 1.5


@pytest.mark.skipif(sys.platform != "linux", reason="PySide6 is a Linux-only dependency")
def test_linux_ui_never_loads_qtnetwork():
    code = (
        "import sys, lookfocus\n"
        "import lookfocus.linux.tray, lookfocus.linux.overlay, lookfocus.linux.viewwindow\n"
        "import lookfocus.linux.kwin, lookfocus.linux.sysevents\n"
        "net = ('urllib3', 'requests', 'http.client')\n"
        "bad = [m for m in sys.modules if 'QtNetwork' in m or m.startswith(net)]\n"
        "print(json.dumps(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", "import json\n" + code], capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
