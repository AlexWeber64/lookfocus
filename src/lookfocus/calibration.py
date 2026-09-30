"""Calibration data: where each logical display sits in head-pose space.

For every logical display the user looks at 5 dots (centre and near each edge); each dot's ~2 s of pose
samples are reduced to a robust median (MAD outlier rejection). Medians are stored for *every* pose
estimator, so switching pose_source in config doesn't need a recalibration.

Pure logic + JSON persistence; the overlay/capture driving it lives in overlay.py / main.py.
Stored at %APPDATA%\\LookFocus\\calibration.json, keyed by logical-display id (monitor device path,
resolution, position, and zone).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from lookfocus.displays import LogicalDisplay, Rect, layout_diff

VERSION = 1
SOURCES = ("matrix", "lm3d", "lm2d")

# (name, fx, fy): fractions of the display. Edge dots sit a little inside the edges so they are fully
# visible; the classifier extrapolates to the true edges.
POINTS = (
    ("center", 0.5, 0.5),
    ("left", 0.06, 0.5),
    ("right", 0.94, 0.5),
    ("top", 0.5, 0.08),
    ("bottom", 0.5, 0.92),
)


def robust_center(values: list[float], k: float = 3.0) -> tuple[float, float, int]:
    """(median, robust sd, samples kept) after dropping points more than k robust sd from the median."""
    vals = [v for v in values if math.isfinite(v)]
    if not vals:
        return math.nan, math.nan, 0
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    sd = 1.4826 * mad
    if sd > 0:
        kept = [v for v in vals if abs(v - med) <= k * sd]
        med = statistics.median(kept)
        sd = 1.4826 * statistics.median(abs(v - med) for v in kept)
    else:
        kept = vals
    return med, sd, len(kept)


@dataclass
class PointCalibration:
    name: str
    fx: float
    fy: float
    n: int  # samples kept for the primary source
    pose: dict[str, tuple[float, float]]  # source -> (horizontal, vertical) median
    spread: dict[str, tuple[float, float]]  # source -> robust sd per axis

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "fx": self.fx,
            "fy": self.fy,
            "n": self.n,
            "pose": {k: list(v) for k, v in self.pose.items()},
            "spread": {k: list(v) for k, v in self.spread.items()},
        }

    @classmethod
    def from_json(cls, d: dict) -> PointCalibration:
        return cls(
            name=d["name"],
            fx=float(d["fx"]),
            fy=float(d["fy"]),
            n=int(d["n"]),
            pose={k: (float(v[0]), float(v[1])) for k, v in d["pose"].items()},
            spread={k: (float(v[0]), float(v[1])) for k, v in d["spread"].items()},
        )


def summarize_point(
    name: str, fx: float, fy: float, samples: list[dict[str, tuple[float, float]]], primary: str = "matrix"
) -> PointCalibration:
    """Reduce per-frame {source: (h, v)} samples to robust medians per source."""
    pose, spread = {}, {}
    n_primary = 0
    for src in SOURCES:
        hs = [s[src][0] for s in samples if src in s]
        vs = [s[src][1] for s in samples if src in s]
        h, sh, nh = robust_center(hs)
        v, sv, nv = robust_center(vs)
        if nh and nv:
            pose[src] = (h, v)
            spread[src] = (sh, sv)
            if src == primary:
                n_primary = min(nh, nv)
    return PointCalibration(name, fx, fy, n_primary, pose, spread)


@dataclass
class DisplayCalibration:
    id: str
    label: str
    rect: Rect
    points: list[PointCalibration]

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "rect": self.rect.as_list(),
            "points": [p.to_json() for p in self.points],
        }

    @classmethod
    def from_json(cls, d: dict) -> DisplayCalibration:
        return cls(
            d["id"],
            d["label"],
            Rect.from_list(d["rect"]),
            [PointCalibration.from_json(p) for p in d["points"]],
        )


@dataclass
class Calibration:
    displays: list[DisplayCalibration]
    zones: int = 0
    created: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    version: int = VERSION

    @property
    def display_ids(self) -> list[str]:
        return [d.id for d in self.displays]

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "created": self.created,
            "zones": self.zones,
            "displays": [d.to_json() for d in self.displays],
        }

    @classmethod
    def from_json(cls, d: dict) -> Calibration:
        if int(d.get("version", 0)) != VERSION:
            raise ValueError(f"unsupported calibration version {d.get('version')!r}")
        return cls(
            displays=[DisplayCalibration.from_json(x) for x in d["displays"]],
            zones=int(d.get("zones", 0)),
            created=str(d.get("created", "")),
        )

    def check_layout(self, displays: list[LogicalDisplay]) -> tuple[list[str], list[str]]:
        """(calibrated-but-missing, present-but-uncalibrated) display ids. Both empty = layout unchanged."""
        return layout_diff(self.display_ids, displays)


def save(cal: Calibration, path: Path) -> None:
    """Atomic write: a crash mid-save never leaves a truncated calibration."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".calibration-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cal.to_json(), f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load(path: Path) -> Calibration | None:
    if not path.exists():
        return None
    return Calibration.from_json(json.loads(path.read_text(encoding="utf-8")))
