import itertools
import math

import numpy as np
import pytest

from lookfocus import pose


def ry(a):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def rx(a):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def rz(a):
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def rot(yaw, pitch, roll):
    return ry(yaw) @ rx(pitch) @ rz(roll)


def rigid(r, t=(1.5, -2.0, -45.0), scale=1.0, layout="row"):
    m = np.eye(4)
    m[:3, :3] = r * scale
    m[:3, 3] = t
    return m if layout == "row" else m.T


FLIP_MODEL_YZ = np.diag([1.0, -1.0, -1.0])  # model axes y and z point the other way
ANGLES = [(y, p, r) for y in (-50, -20, 0, 15, 45) for p in (-30, 0, 25) for r in (-20, 0, 10)]


class TestLayout:
    def test_row_major(self):
        assert pose.detect_layout(rigid(rot(10, 5, 0))) == "row"

    def test_column_major(self):
        assert pose.detect_layout(rigid(rot(10, 5, 0), layout="col")) == "col"

    def test_projective_is_unknown(self):
        m = rigid(rot(10, 5, 0))
        m[3, :] = [0.1, 0, 0, 1]
        m[:, 3] = [1, 2, 3, 1]
        assert pose.detect_layout(m) == "unknown"

    def test_flat_16_values_accepted(self):
        m = rigid(rot(10, 5, 0), layout="col")
        assert pose.detect_layout(m.ravel()) == "col"

    @pytest.mark.parametrize("layout", ["row", "col"])
    def test_rigid_parts_recovers_rotation_translation_and_strips_scale(self, layout):
        r_true = rot(30, -10, 5)
        r, t, got = pose.rigid_parts(rigid(r_true, t=(3, 4, -50), scale=1.7, layout=layout))
        assert got == layout
        np.testing.assert_allclose(r, r_true, atol=1e-9)
        np.testing.assert_allclose(t, [3, 4, -50], atol=1e-9)


class TestMatrixAngles:
    @pytest.mark.parametrize(("yaw", "pitch", "roll"), ANGLES)
    def test_recovers_yxz_euler(self, yaw, pitch, roll):
        got = pose.matrix_angles(rot(yaw, pitch, roll))
        np.testing.assert_allclose(got, (yaw, pitch, roll), atol=1e-9)

    @pytest.mark.parametrize(("yaw", "pitch", "roll"), ANGLES)
    def test_model_axis_sign_convention_does_not_matter(self, yaw, pitch, roll):
        base = pose.matrix_angles(rot(yaw, pitch, roll))
        flipped = pose.matrix_angles(rot(yaw, pitch, roll) @ FLIP_MODEL_YZ)
        np.testing.assert_allclose(flipped[:2], base[:2], atol=1e-9)
        assert abs(flipped[2]) == pytest.approx(abs(base[2]), abs=1e-9)  # roll: diagnostic, sign may flip

    @pytest.mark.parametrize(("yaw", "pitch", "roll"), ANGLES)
    def test_camera_axis_convention_only_flips_signs(self, yaw, pitch, roll):
        # OpenGL-style (y up, z toward viewer) vs OpenCV-style (y down, z forward) camera frames differ by
        # a 180-degree turn about camera x. That mirrors the horizontal angle and leaves the rest alone.
        y, p, r = pose.matrix_angles(np.diag([1.0, -1.0, -1.0]) @ rot(yaw, pitch, roll))
        assert y == pytest.approx(-yaw, abs=1e-9)
        assert p == pytest.approx(pitch, abs=1e-9)
        assert abs(r) == pytest.approx(abs(roll), abs=1e-9)

    def test_yaw_pitch_ignore_roll(self):
        a = pose.matrix_angles(rot(25, -12, 0))
        b = pose.matrix_angles(rot(25, -12, 30))
        assert a[:2] == pytest.approx(b[:2], abs=1e-9)

    @pytest.mark.parametrize("convention", [np.eye(3), FLIP_MODEL_YZ, np.diag([-1.0, 1.0, -1.0])])
    def test_no_wraparound_near_frontal(self, convention):
        yaws = [pose.matrix_angles(rot(a, 0, 0) @ convention)[0] for a in np.linspace(-5, 5, 21)]
        assert max(abs(y) for y in yaws) < 5.0 + 1e-9
        assert all(abs(b - a) < 1.0 for a, b in itertools.pairwise(yaws))


def synthetic_landmarks(r, center=(320.0, 240.0, 0.0), scale=10.0):
    """A few key face points (image coords: x right, y down, z away from camera), rotated by r."""
    pts = np.zeros((478, 3))
    canon = {
        pose.CHEEK_A: (-7, 0, 0),
        pose.CHEEK_B: (7, 0, 0),
        pose.FOREHEAD: (0, -9, 0),
        pose.CHIN: (0, 9, 0),
        pose.NOSE_TIP: (0, 2, -4),
        pose.NASION: (0, -3, -1),
    }
    for idx, p in canon.items():
        pts[idx] = np.asarray(center) + scale * (r @ np.asarray(p, dtype=float))
    return pts


class TestLandmarkEstimators:
    def test_frontal_is_zero(self):
        assert pose.landmark_frame_angles(synthetic_landmarks(np.eye(3))) == pytest.approx((0, 0), abs=1e-9)

    @pytest.mark.parametrize("deg", [-40, -15, 10, 35])
    def test_3d_frame_tracks_yaw_and_pitch_magnitude(self, deg):
        y, p = pose.landmark_frame_angles(synthetic_landmarks(ry(deg)))
        assert abs(y) == pytest.approx(abs(deg), abs=1e-6)
        assert p == pytest.approx(0, abs=1e-6)
        y, p = pose.landmark_frame_angles(synthetic_landmarks(rx(deg)))
        assert abs(p) == pytest.approx(abs(deg), abs=1e-6)
        assert y == pytest.approx(0, abs=1e-6)

    def test_3d_frame_ignores_roll(self):
        a = pose.landmark_frame_angles(synthetic_landmarks(ry(20) @ rx(10)))
        b = pose.landmark_frame_angles(synthetic_landmarks(ry(20) @ rx(10) @ rz(25)))
        assert a == pytest.approx(b, abs=1e-6)

    def test_2d_ratios_are_monotonic_in_head_turn(self):
        hs = [pose.landmark_2d_ratios(synthetic_landmarks(ry(a)))[0] for a in range(-40, 41, 10)]
        vs = [pose.landmark_2d_ratios(synthetic_landmarks(rx(a)))[1] for a in range(-40, 41, 10)]
        for seq in (hs, vs):
            diffs = np.diff(seq)
            assert np.all(diffs > 0) or np.all(diffs < 0)

    def test_isotropic_scaling(self):
        p = pose.landmarks_to_isotropic(np.array([[0.5, 0.25, 0.1]]), 640, 480)
        np.testing.assert_allclose(p, [[320, 120, 64]])


class TestHeadPose:
    def test_combines_estimators(self):
        r = rot(20, -10, 5)
        norm = synthetic_landmarks(ry(20) @ rx(-10))
        norm[:, 0] /= 640
        norm[:, 1] /= 480
        norm[:, 2] /= 640
        hp = pose.head_pose(rigid(r, layout="col"), norm, 640, 480)
        assert hp.layout == "col"
        assert (hp.yaw, hp.pitch, hp.roll) == pytest.approx((20, -10, 5), abs=1e-6)
        assert abs(hp.lm_yaw) == pytest.approx(20, abs=1e-6)
        assert hp.tz == pytest.approx(-45)
        assert len(hp.matrix) == 16
        assert hp.features("matrix") == (hp.yaw, hp.pitch)
        assert hp.features("hybrid") == (hp.yaw, hp.lm_pitch)

    def test_without_matrix(self):
        hp = pose.head_pose(None, synthetic_landmarks(np.eye(3)) / 640, 640, 640)
        assert hp.layout == "unknown"
        assert math.isnan(hp.yaw)

    def test_unknown_source_rejected(self):
        hp = pose.head_pose(None, synthetic_landmarks(np.eye(3)) / 640, 640, 640)
        with pytest.raises(ValueError):
            hp.features("iris")
