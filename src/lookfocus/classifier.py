"""Which logical display is the head pointed at?

**Nearest calibrated centroid, normalized distance.** Every calibration dot's median pose is a prototype
labelled with its display, and a pose goes to the display owning the nearest prototype. Because each display
contributes edge dots as well as its centre, the boundary between two adjacent displays falls midway between
one's edge dot and the other's (at the bezel), even when the displays differ in size. Centres alone would put
it halfway between the centres.

**Normalization:** each axis is divided by max(pooled calibration noise, 3% of the calibrated extent on that
axis), so yaw and pitch weigh comparably and any estimator's units work (degrees or unitless ratios).

**"none":** each display also has a calibrated *region*, the least-squares affine map from screen fraction
(fx, fy) to pose fitted to its dots and applied to the whole rectangle. A pose further than `none_margin`
(normalized units) outside every region is "none": phone, desk, window, another person.

No sign conventions are assumed anywhere: only distances between measured poses are used.
Pure logic (numpy only).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from lookfocus.calibration import Calibration

# (fx, fy, horizontal, vertical, point name)
Proto = tuple[float, float, float, float, str]


@dataclass(frozen=True)
class Result:
    display: str | None  # None = facing none of the displays
    distance: dict[str, float]  # display id -> normalized distance to its nearest prototype
    outside: dict[str, float]  # display id -> normalized distance outside its region (0 = inside)
    position: tuple[float, float] | None = None  # (fx, fy) inside the chosen display, from its region
    nearest_point: str = ""


class Region:
    """pose = A @ (fx, fy) + b, fitted to one display's calibration dots."""

    def __init__(self, protos: list[Proto]) -> None:
        self.ok = False
        if len(protos) < 3:
            return
        x = np.array([[p[0], p[1], 1.0] for p in protos])
        y = np.array([[p[2], p[3]] for p in protos])
        if np.linalg.matrix_rank(x) < 3:
            return
        w, *_ = np.linalg.lstsq(x, y, rcond=None)
        self.a = w[:2].T  # 2x2: columns = pose change per unit fx, per unit fy
        self.b = w[2]
        if abs(np.linalg.det(self.a)) < 1e-12 or np.linalg.cond(self.a) > 1e6:
            return
        self.a_inv = np.linalg.inv(self.a)
        self.ok = True

    def fractions(self, p: np.ndarray) -> np.ndarray:
        return self.a_inv @ (p - self.b)

    def outside(self, p: np.ndarray, scale: np.ndarray) -> float:
        f = self.fractions(p)
        q = np.clip(f, 0.0, 1.0)
        return float(np.linalg.norm((self.a @ (f - q)) / scale))


class Classifier:
    def __init__(
        self,
        displays: dict[str, list[Proto]],
        noise: tuple[float, float] | None = None,
        none_margin: float = 6.0,
    ) -> None:
        if not displays or not any(displays.values()):
            raise ValueError("classifier needs at least one calibrated display")
        self.displays = {k: v for k, v in displays.items() if v}
        self.none_margin = none_margin
        all_pts = np.array([[p[2], p[3]] for ps in self.displays.values() for p in ps])
        extent = all_pts.max(axis=0) - all_pts.min(axis=0)
        floor = 0.03 * extent
        n = np.array(noise) if noise is not None else np.zeros(2)
        self.scale = np.maximum(np.maximum(n, floor), 1e-6)
        self.regions = {k: Region(v) for k, v in self.displays.items()}
        self._protos = [
            (k, np.array([p[2], p[3]]) / self.scale, p[4]) for k, v in self.displays.items() for p in v
        ]
        # Fallback reach for displays without a usable region: farthest dot from the display's mean.
        self._reach = {}
        for k, v in self.displays.items():
            pts = np.array([[p[2], p[3]] for p in v]) / self.scale
            self._reach[k] = (
                float(np.max(np.linalg.norm(pts - pts.mean(axis=0), axis=1))) if len(v) > 1 else 0.0
            )

    @classmethod
    def from_calibration(
        cls, cal: Calibration, source: str = "matrix", none_margin: float = 6.0
    ) -> Classifier:
        displays: dict[str, list[Proto]] = {}
        spreads: list[tuple[float, float]] = []
        for d in cal.displays:
            ps = []
            for p in d.points:
                if source in p.pose and all(math.isfinite(x) for x in p.pose[source]):
                    ps.append((p.fx, p.fy, p.pose[source][0], p.pose[source][1], p.name))
                    if source in p.spread:
                        spreads.append(p.spread[source])
            displays[d.id] = ps
        noise = None
        if spreads:
            noise = (float(np.median([s[0] for s in spreads])), float(np.median([s[1] for s in spreads])))
        return cls(displays, noise=noise, none_margin=none_margin)

    def classify(self, h: float, v: float) -> Result:
        if not (math.isfinite(h) and math.isfinite(v)):
            return Result(None, {}, {})
        p = np.array([h, v])
        pn = p / self.scale
        dist: dict[str, float] = {}
        nearest_name: dict[str, str] = {}
        for k, q, name in self._protos:
            d = float(np.linalg.norm(pn - q))
            if d < dist.get(k, math.inf):
                dist[k], nearest_name[k] = d, name
        outside = {}
        for k, reg in self.regions.items():
            if reg.ok:
                outside[k] = reg.outside(p, self.scale)
            else:
                outside[k] = max(0.0, dist[k] - self._reach[k])
        if min(outside.values()) > self.none_margin:
            return Result(None, dist, outside)
        best = min(dist, key=dist.get)
        reg = self.regions[best]
        pos = tuple(float(x) for x in reg.fractions(p)) if reg.ok else None
        return Result(best, dist, outside, pos, nearest_name[best])
