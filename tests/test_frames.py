import cv2
import numpy as np
import pytest

from lookfocus import calibration as C
from lookfocus.capture import nv12_to_bgr, shrink


def make_nv12(bgr: np.ndarray) -> np.ndarray:
    """BGR -> NV12 buffer (Y plane, then interleaved UV at half resolution), flattened like MSMF raw."""
    h, w = bgr.shape[:2]
    i420 = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420).reshape(-1)
    y = i420[: w * h]
    u = i420[w * h : w * h + w * h // 4]
    v = i420[w * h + w * h // 4 :]
    uv = np.empty(w * h // 2, np.uint8)
    uv[0::2], uv[1::2] = u, v
    return np.concatenate([y, uv]).reshape(1, -1)


@pytest.fixture
def scene():
    rng = np.random.default_rng(0)
    img = cv2.resize(
        rng.integers(40, 215, (9, 16, 3), dtype=np.uint8), (2560, 1440), interpolation=cv2.INTER_CUBIC
    )
    cv2.rectangle(img, (900, 400), (1500, 1100), (40, 90, 200), -1)  # a big coloured block
    return img


def test_nv12_fast_path_matches_reference(scene):
    raw = make_nv12(scene)
    out = nv12_to_bgr(raw, 2560, 1440, 640)
    ref = cv2.resize(scene, (640, 360), interpolation=cv2.INTER_AREA)
    assert out.shape == (360, 640, 3)
    grey = lambda im: cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(int)  # noqa: E731
    assert float(np.abs(grey(out) - grey(ref)).mean()) < 2  # luma is area-averaged: near exact
    assert float(np.abs(out.astype(int) - ref.astype(int)).mean()) < 5  # chroma is subsampled
    block = out[130:250, 260:340].reshape(-1, 3).mean(0)
    np.testing.assert_allclose(block, [40, 90, 200], atol=12)  # colours land in the right channels


def test_nv12_full_size_when_already_small():
    img = np.full((480, 640, 3), (10, 120, 230), np.uint8)
    out = nv12_to_bgr(make_nv12(img), 640, 480, 640)
    assert out.shape == (480, 640, 3)
    np.testing.assert_allclose(out.reshape(-1, 3).mean(0), [10, 120, 230], atol=4)


def test_nv12_indivisible_size_falls_back(scene):
    img = cv2.resize(scene, (1366, 768))
    out = nv12_to_bgr(make_nv12(img), 1366, 768, 640)
    assert out.shape[1] == 640


def test_shrink():
    big = np.zeros((1080, 1920, 3), np.uint8)
    assert shrink(big, 640).shape == (360, 640, 3)
    small = np.zeros((480, 640, 3), np.uint8)
    assert shrink(small, 640) is small


def test_calibration_records_camera_mode(tmp_path):
    cal = C.Calibration([], zones=2, camera=(2560, 1440))
    p = tmp_path / "c.json"
    C.save(cal, p)
    back = C.load(p)
    assert back.camera == (2560, 1440)
    assert back.camera_mismatch((640, 480)) and not back.camera_mismatch((2560, 1440))
    assert not back.camera_mismatch((0, 0))  # unknown frame size: no warning
    assert not C.Calibration([], camera=None).camera_mismatch((640, 480))  # old calibration: no info
