import math

import numpy as np
import pytest

from lookfocus.calibration import POINTS
from lookfocus.classifier import Classifier


def display(h0, h1, v0, v1, points=POINTS, jitter=0.0, rng=None):
    """Synthetic calibration: pose is linear in screen position (h across, v down)."""
    out = []
    for name, fx, fy in points:
        dh = rng.normal(0, jitter) if rng is not None else 0.0
        dv = rng.normal(0, jitter) if rng is not None else 0.0
        out.append((fx, fy, h0 + fx * (h1 - h0) + dh, v0 + fy * (v1 - v0) + dv, name))
    return out


def side_by_side():
    # A: yaw -40..-10, B: yaw -5..25 (5 degree "bezel" gap); both pitch -10..+10
    return {"A": display(-40, -10, -10, 10), "B": display(-5, 25, -10, 10)}


def pose_on(proto_display, fx, fy):
    """Pose of a point on a synthetic display, via its own linear map (from the centre/right/bottom dots)."""
    c = next(p for p in proto_display if p[4] == "center")
    r = next(p for p in proto_display if p[4] == "right")
    b = next(p for p in proto_display if p[4] == "bottom")
    dh = (r[2] - c[2]) / (r[0] - c[0])
    dv = (b[3] - c[3]) / (b[1] - c[1])
    return c[2] + (fx - 0.5) * dh, c[3] + (fy - 0.5) * dv


class TestNearestCentroid:
    def test_centres_and_edges(self):
        cal = side_by_side()
        clf = Classifier(cal, noise=(1.0, 1.0))
        for name in ("A", "B"):
            for fx, fy in ((0.5, 0.5), (0.02, 0.5), (0.98, 0.5), (0.5, 0.02), (0.5, 0.98), (0.05, 0.05)):
                assert clf.classify(*pose_on(cal[name], fx, fy)).display == name, (name, fx, fy)

    def test_boundary_sits_in_the_bezel(self):
        clf = Classifier(side_by_side(), noise=(1.0, 1.0))
        assert clf.classify(-10.5, 0).display == "A"  # just inside A's right edge
        assert clf.classify(-4.5, 0).display == "B"  # just inside B's left edge

    def test_edge_points_beat_centres_for_unequal_sizes(self):
        # A is narrow (-20..-10), B is wide (-8..52). Halfway between the *centres* (-15, 22) is 3.5,
        # which is well inside B; the edge dots put the boundary near the real bezel (-9).
        cal = {"A": display(-20, -10, -10, 10), "B": display(-8, 52, -10, 10)}
        clf = Classifier(cal, noise=(1.0, 1.0))
        assert clf.classify(0.0, 0.0).display == "B"
        assert clf.classify(-10.5, 0.0).display == "A"

    def test_stacked_vertically(self):
        cal = {"top": display(-20, 20, -35, -12), "bottom": display(-20, 20, -8, 15)}
        clf = Classifier(cal, noise=(1.0, 1.0))
        assert clf.classify(0, -14).display == "top"
        assert clf.classify(0, -6).display == "bottom"
        assert clf.classify(18, 13).display == "bottom"

    def test_sign_conventions_do_not_matter(self):
        cal = side_by_side()
        flipped = {k: [(fx, fy, -h, -v, n) for fx, fy, h, v, n in ps] for k, ps in cal.items()}
        a, b = Classifier(cal, noise=(1, 1)), Classifier(flipped, noise=(1, 1))
        rng = np.random.default_rng(3)
        for h, v in rng.uniform(-45, 30, size=(200, 2)):
            assert a.classify(h, v).display == b.classify(-h, -v).display

    def test_three_zones_on_one_screen_with_noisy_calibration(self):
        rng = np.random.default_rng(7)
        cal = {
            "z1": display(-18, -7, -6, 8, jitter=0.4, rng=rng),
            "z2": display(-6, 5, -6, 8, jitter=0.4, rng=rng),
            "z3": display(6, 17, -6, 8, jitter=0.4, rng=rng),
        }
        clf = Classifier(cal, noise=(1.0, 0.3))
        assert [clf.classify(h, 1).display for h in (-12, 0, 11)] == ["z1", "z2", "z3"]


class TestNone:
    def test_far_below_is_none(self):
        clf = Classifier(side_by_side(), noise=(1.0, 1.0), none_margin=6.0)
        r = clf.classify(-20, 45)  # phone on the desk
        assert r.display is None
        assert min(r.outside.values()) > 6

    def test_slightly_outside_still_counts(self):
        clf = Classifier(side_by_side(), noise=(1.0, 1.0), none_margin=6.0)
        assert clf.classify(-20, 13).display == "A"  # 3 degrees below the bottom edge

    def test_far_to_the_side_is_none(self):
        clf = Classifier(side_by_side(), noise=(1.0, 1.0), none_margin=6.0)
        assert clf.classify(60, 0).display is None
        assert clf.classify(-80, 0).display is None

    def test_margin_is_in_normalized_units(self):
        clf = Classifier(side_by_side(), noise=(1.0, 1.0), none_margin=6.0)
        # yaw scale = 3% of the dots' extent (61.4 deg) = 1.842 deg; pitch scale = noise = 1 deg
        assert clf.classify(-50, 0).display == "A"  # 10 deg left of A = 5.4 units
        assert clf.classify(-53, 0).display is None  # 13 deg = 7.1 units
        assert clf.classify(-25, 15).display == "A"  # 5 deg below = 5 units
        assert clf.classify(-25, 17).display is None  # 7 deg below = 7 units


class TestResultDetails:
    def test_position_within_display(self):
        cal = side_by_side()
        clf = Classifier(cal, noise=(1, 1))
        r = clf.classify(*pose_on(cal["B"], 0.25, 0.75))
        assert r.display == "B"
        assert r.position == pytest.approx((0.25, 0.75), abs=1e-6)
        assert r.outside["B"] == 0.0

    def test_distances_for_every_display(self):
        clf = Classifier(side_by_side(), noise=(1, 1))
        r = clf.classify(-25, 0)
        assert set(r.distance) == {"A", "B"} and r.distance["A"] < r.distance["B"]
        assert r.nearest_point == "center"

    def test_nan_pose_is_none(self):
        clf = Classifier(side_by_side())
        assert clf.classify(math.nan, 0).display is None

    def test_degenerate_display_falls_back_to_reach(self):
        cal = {"A": [(0.5, 0.5, -20, 0, "center")], "B": display(-5, 25, -10, 10)}
        clf = Classifier(cal, noise=(1, 1), none_margin=4)
        assert not clf.regions["A"].ok
        assert clf.classify(-19, 1).display == "A"
        assert clf.classify(-60, 0).display is None

    def test_scale_floor_uses_extent(self):
        clf = Classifier(side_by_side(), noise=(0.1, 0.1))
        np.testing.assert_allclose(clf.scale, [0.03 * 61.4, 0.03 * 20 * 0.84], rtol=1e-6)

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            Classifier({})
