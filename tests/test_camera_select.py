import threading
import time

from lookfocus.camera_enum import CameraChoice, CameraDevice, candidate_indices, device_key
from lookfocus.capture import LatestSlot

# Real paths from the dev machine: the same camera as seen by MSMF and by DSHOW.
MSMF_PATH = (
    r"\\?\display#int3480#4&57c2379&0&uid144512#{e5323777-f976-4f5b-9b55-b94699c46e44}"
    r"\{fffb7209-1b4b-435f-91d4-3c134734a5a2}"
)
DSHOW_PATH = (
    r"\\?\display#int3480#4&57c2379&0&uid144512#{65e8773d-8f56-11d0-a3b9-00a0c9223196}"
    r"\{fffb7209-1b4b-435f-91d4-3c134734a5a2}"
)

DSHOW = [
    CameraDevice("dshow", 0, "Integrated Camera", DSHOW_PATH),
    CameraDevice("dshow", 1, "OBS Virtual Camera", ""),
]


def test_device_key_matches_across_backends():
    assert device_key(MSMF_PATH) == device_key(DSHOW_PATH)
    assert device_key(MSMF_PATH) != device_key(MSMF_PATH.replace("uid144512", "uid999"))


def test_choice_by_path_from_other_backend():
    assert candidate_indices(DSHOW, CameraChoice(path=MSMF_PATH)) == ([0], True)


def test_choice_by_name_case_insensitive():
    assert candidate_indices(DSHOW, CameraChoice(name="obs virtual camera")) == ([1], True)


def test_choice_by_index():
    assert candidate_indices(DSHOW, CameraChoice(index=1)) == ([1], True)


def test_missing_configured_camera_falls_back_to_auto():
    assert candidate_indices(DSHOW, CameraChoice(path=r"\\?\usb#vid_046d#gone")) == ([0, 1], False)


def test_auto_puts_ir_named_devices_last():
    devs = [
        CameraDevice("msmf", 0, "Integrated IR Camera", "a"),
        CameraDevice("msmf", 1, "Integrated Camera", "b"),
    ]
    assert candidate_indices(devs, CameraChoice()) == ([1, 0], False)


def test_auto_with_no_enumeration_tries_index_0():
    assert candidate_indices([], CameraChoice()) == ([0], False)


def test_ir_name_hint():
    assert CameraDevice("msmf", 0, "Windows Hello Face", "").looks_ir
    assert CameraDevice("msmf", 0, "Integrated IR Camera", "").looks_ir
    assert not CameraDevice("msmf", 0, "Integrated Camera", "").looks_ir
    assert not CameraDevice("msmf", 0, "Brio 4K Stream", "").looks_ir


class TestLatestSlot:
    def test_keeps_only_newest(self):
        slot = LatestSlot()
        for i in range(5):
            slot.put(i)
        assert slot.get() == (5, 4)

    def test_wait_newer_times_out_without_update(self):
        slot = LatestSlot()
        slot.put("a")
        seq, _ = slot.get()
        t = time.monotonic()
        assert slot.wait_newer(seq, 0.05) == (seq, "a")
        assert time.monotonic() - t >= 0.04

    def test_wait_newer_wakes_on_put(self):
        slot = LatestSlot()
        threading.Timer(0.02, slot.put, args=("b",)).start()
        seq, value = slot.wait_newer(0, 2.0)
        assert (seq, value) == (1, "b")
