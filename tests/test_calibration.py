import json
import math

import pytest

from lookfocus import calibration as C
from lookfocus.classifier import Classifier
from lookfocus.displays import LayoutError, Monitor, Rect, build_displays, display_for_point, require_multiple

LAPTOP = Monitor(
    device=r"\\.\DISPLAY1",
    path=r"\\?\DISPLAY#LEN4220#4&57c2379&0&UID8388688#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}",
    name="",
    rect=Rect(0, 0, 2880, 1800),
    work=Rect(0, 0, 2880, 1704),
    primary=True,
    dpi=192,
)
EXTERNAL = Monitor(
    r"\\.\DISPLAY2",
    r"\\?\DISPLAY#DEL41B8#x",
    "DELL U2720Q",
    Rect(2880, -400, 6720, 1760),
    Rect(2880, -400, 6720, 1720),
    False,
    144,
)


class TestRobustCenter:
    def test_rejects_outliers(self):
        vals = [10.0, 10.2, 9.9, 10.1, 9.8, 10.0, 30.0, -20.0]
        med, sd, kept = C.robust_center(vals)
        assert med == pytest.approx(10.0, abs=0.1)
        assert kept == 6
        assert sd < 0.5

    def test_constant_values(self):
        assert C.robust_center([5.0] * 10) == (5.0, 0.0, 10)

    def test_ignores_nan_and_empty(self):
        med, _, kept = C.robust_center([math.nan, 1.0, 1.0, 1.0])
        assert (med, kept) == (1.0, 3)
        assert math.isnan(C.robust_center([])[0])

    def test_summarize_point_all_sources(self):
        samples = [
            {"matrix": (10 + i * 0.01, -5.0), "lm3d": (-12.0, 3.0), "lm2d": (0.1, -0.2)} for i in range(30)
        ]
        samples.append({"matrix": (80.0, 40.0), "lm3d": (-12.0, 3.0), "lm2d": (0.1, -0.2)})  # blink/glitch
        p = C.summarize_point("center", 0.5, 0.5, samples)
        assert p.pose["matrix"][0] == pytest.approx(10.145, abs=0.02)
        assert p.pose["lm3d"] == (-12.0, 3.0)
        assert p.n == 30


def make_cal(displays):
    out = []
    for i, d in enumerate(displays):
        pts = []
        for name, fx, fy in C.POINTS:
            h, v = -30 + 40 * i + 30 * fx, -10 + 20 * fy
            pts.append(
                C.PointCalibration(
                    name,
                    fx,
                    fy,
                    30,
                    {"matrix": (h, v), "lm3d": (-h, v)},
                    {"matrix": (0.8, 0.3), "lm3d": (1.0, 0.3)},
                )
            )
        out.append(C.DisplayCalibration(d.id, d.label, d.rect, pts))
    return C.Calibration(out, zones=2)


class TestPersistence:
    def test_round_trip(self, tmp_path):
        ds = build_displays([LAPTOP], zones=2)
        cal = make_cal(ds)
        path = tmp_path / "LookFocus" / "calibration.json"
        C.save(cal, path)
        back = C.load(path)
        assert back.to_json() == cal.to_json()
        assert json.loads(path.read_text())["version"] == C.VERSION

    def test_no_temp_files_left(self, tmp_path):
        C.save(make_cal(build_displays([LAPTOP], zones=2)), tmp_path / "calibration.json")
        assert [p.name for p in tmp_path.iterdir()] == ["calibration.json"]

    def test_missing_file(self, tmp_path):
        assert C.load(tmp_path / "nope.json") is None

    def test_rejects_other_versions(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"version": 99, "displays": []}))
        with pytest.raises(ValueError):
            C.load(p)

    def test_classifier_from_calibration_any_source(self):
        ds = build_displays([LAPTOP], zones=2)
        cal = make_cal(ds)
        for source, sign in (("matrix", 1), ("lm3d", -1)):
            clf = Classifier.from_calibration(cal, source)
            assert clf.classify(sign * -15, 0).display == ds[0].id
            assert clf.classify(sign * 25, 0).display == ds[1].id
        clf = Classifier.from_calibration(cal, "matrix")
        assert clf.scale[0] == pytest.approx(0.03 * (40 + 30 * 0.88))  # extent floor beats 0.8 noise


class TestLayout:
    def test_unchanged(self):
        ds = build_displays([LAPTOP], zones=2)
        assert make_cal(ds).check_layout(ds) == ([], [])

    def test_monitor_added(self):
        cal = make_cal(build_displays([LAPTOP], zones=2))
        missing, added = cal.check_layout(build_displays([LAPTOP, EXTERNAL], zones=2))
        assert missing == [] and len(added) == 1

    def test_zones_changed(self):
        cal = make_cal(build_displays([LAPTOP], zones=2))
        missing, added = cal.check_layout(build_displays([LAPTOP], zones=3))
        assert len(missing) == 2 and len(added) == 3

    def test_monitor_moved_or_resized(self):
        cal = make_cal(build_displays([LAPTOP, EXTERNAL]))
        moved = Monitor(
            EXTERNAL.device, EXTERNAL.path, EXTERNAL.name, Rect(-3840, 0, 0, 2160), EXTERNAL.work, False
        )
        missing, added = cal.check_layout(build_displays([LAPTOP, moved]))
        assert len(missing) == 1 and len(added) == 1


class TestDisplays:
    def test_zones_split_primary_only(self):
        ds = build_displays([EXTERNAL, LAPTOP], zones=2)
        assert [d.zone for d in ds] == [1, 2, 0]
        assert ds[0].rect == Rect(0, 0, 1440, 1800) and ds[1].rect == Rect(1440, 0, 2880, 1800)
        assert "left half" in ds[0].label and "DELL U2720Q" in ds[2].label

    def test_three_zones_cover_exactly(self):
        ds = build_displays([LAPTOP], zones=3)
        assert [d.rect.left for d in ds] == [0, 960, 1920] and ds[-1].rect.right == 2880

    def test_ordered_left_to_right(self):
        left = Monitor("a", "pa", "Left", Rect(-1920, 0, 0, 1080), Rect(-1920, 0, 0, 1040), False)
        ds = build_displays([LAPTOP, left])
        assert [d.label.split(" (")[0] for d in ds] == ["Monitor 1", "Monitor 2"]
        assert "Left" in ds[0].label

    def test_single_monitor_requires_zones(self):
        with pytest.raises(LayoutError, match="--zones 2"):
            require_multiple(build_displays([LAPTOP]), 0)
        require_multiple(build_displays([LAPTOP], zones=2), 2)

    def test_bad_zone_count(self):
        with pytest.raises(LayoutError):
            build_displays([LAPTOP], zones=1)

    def test_window_centre_mapping(self):
        ds = build_displays([LAPTOP, EXTERNAL], zones=2)
        assert display_for_point(ds, 100, 100).zone == 1
        assert display_for_point(ds, 2000, 900).zone == 2
        assert display_for_point(ds, 4000, -300).monitor_key == EXTERNAL.key
        assert display_for_point(ds, 1500, 5000).zone == 2  # off-screen below -> nearest
        assert display_for_point(ds, 1439.5, 10).zone == 1

    def test_monitor_key_includes_resolution_and_position(self):
        assert LAPTOP.key.endswith("|2880x1800@0,0")

    def test_point_at(self):
        r = Rect(1440, 0, 2880, 1800)
        assert r.point_at(0, 0) == (1440, 0) and r.point_at(1, 1) == (2879, 1799)


class TestSeparability:
    def test_good_calibration_has_no_problems(self):
        from lookfocus.calibrator import separability

        problems, report = separability(make_cal(build_displays([LAPTOP], zones=2)), "matrix")
        assert problems == [] and len(report) == 1 and "40.0 apart" in report[0]

    def test_eyes_only_calibration_is_flagged(self):
        from lookfocus.calibrator import separability

        cal = make_cal(build_displays([LAPTOP], zones=2))
        for d in cal.displays:  # head barely turned between zones
            for p in d.points:
                p.pose["matrix"] = (p.pose["matrix"][0] * 0.02, p.pose["matrix"][1])
        problems, _ = separability(cal, "matrix")
        assert any("calibration noise apart" in x for x in problems)
