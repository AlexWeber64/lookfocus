"""Interactive calibration: show dots on each logical display in turn and record head pose.

Drives overlay.Overlay (on the calling thread) and reads poses from a running CaptureThread's LatestSlot.
For each display: 5 dots (calibration.POINTS). Each dot is shown for ~0.9 s to settle, then samples are
collected for 2 s. A dot is retried if too few frames had a face, or if the head wasn't still.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from lookfocus import calibration as C
from lookfocus import overlay as O
from lookfocus.capture import LatestSlot
from lookfocus.classifier import Classifier
from lookfocus.displays import LogicalDisplay
from lookfocus.pose import HeadPose, PoseSample

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalibratorSettings:
    settle_s: float = 0.9
    collect_s: float = 2.0
    min_samples: int = 12  # of ~30 at 15 fps
    max_spread: float = 4.0  # robust sd (primary source units) above which a dot is redone
    retries: int = 2
    face_timeout_s: float = 20.0
    source: str = "matrix"


class Cancelled(Exception):
    pass


def features(p: HeadPose) -> dict[str, tuple[float, float]]:
    return {"matrix": (p.yaw, p.pitch), "lm3d": (p.lm_yaw, p.lm_pitch), "lm2d": (p.lm2d_yaw, p.lm2d_pitch)}


class Calibrator:
    def __init__(
        self,
        slot: LatestSlot[PoseSample],
        overlay: O.Overlay,
        settings: CalibratorSettings,
        say: Callable[[str], None] = print,
        *,
        cancel_hint: str = "Ctrl+C in the terminal (or Esc here) cancels.",
        focus: Callable[[int], object] | None = None,
    ) -> None:
        self.slot, self.ov, self.s, self.say = slot, overlay, settings, say
        self.cancel_hint = cancel_hint
        self.focus = focus  # brings the overlay to the foreground so Esc reaches it (tray: no terminal)
        self.frames: dict[tuple[int, int], int] = {}  # native camera frame sizes seen while collecting

    def _show(self, rect) -> None:
        self.ov.show(rect)
        if self.focus is not None:
            self.focus(self.ov.hwnd)

    def _pump(self, seconds: float) -> None:
        if not self.ov.pump(seconds):
            raise Cancelled()

    def wait_for_face(self, first: LogicalDisplay) -> None:
        self._show(first.rect)
        t_end = time.monotonic() + self.s.face_timeout_s
        while time.monotonic() < t_end:
            _, sample = self.slot.get()
            status = sample.status if sample else "starting"
            if sample is not None and sample.status == "tracking":
                return
            detail = sample.message if sample and sample.message else f"camera: {status}"
            self.ov.set_scene(
                title="Looking for your face...",
                lines=[
                    "Sit as you normally do and face this screen.",
                    detail,
                    self.cancel_hint,
                ],
            )
            self._pump(0.2)
        raise TimeoutError("no face seen; check the camera (tools\\debug_pose.py) and lighting")

    def collect(
        self, display: LogicalDisplay, index: int, name: str, fx: float, fy: float, total: int
    ) -> C.PointCalibration:
        head = f"{display.short}: dot {index}/{total}"
        hint = "Look at the dot, turning your head naturally, as you would when working here."
        for attempt in range(self.s.retries + 1):
            self.ov.set_scene(dot=(fx, fy), color=O.WAITING, title=head, lines=[hint], progress=0.0)
            self._pump(self.s.settle_s)
            samples: list[dict[str, tuple[float, float]]] = []
            seen, _ = self.slot.get()
            t0 = time.monotonic()
            while (el := time.monotonic() - t0) < self.s.collect_s:
                self.ov.set_scene(
                    dot=(fx, fy), color=O.MEASURING, title=head, lines=[hint], progress=el / self.s.collect_s
                )
                self._pump(0.03)
                seq, sample = self.slot.get()
                if seq != seen and sample is not None:
                    seen = seq
                    if sample.pose is not None:
                        samples.append(features(sample.pose))
                        self.frames[sample.frame] = self.frames.get(sample.frame, 0) + 1
            point = C.summarize_point(name, fx, fy, samples, primary=self.s.source)
            spread = point.spread.get(self.s.source, (float("inf"),) * 2)
            if point.n >= self.s.min_samples and max(spread) <= self.s.max_spread:
                log.info(
                    "%s %s: n=%d pose=%s sd=%s",
                    display.short,
                    name,
                    point.n,
                    point.pose.get(self.s.source),
                    spread,
                )
                return point
            why = (
                "couldn't see your face well enough"
                if point.n < self.s.min_samples
                else "your head was moving; hold still while the dot is green"
            )
            log.info(
                "%s %s: retry %d (%s; n=%d sd=%s)", display.short, name, attempt + 1, why, point.n, spread
            )
            self.ov.set_scene(
                dot=None, title=f"Again: {why}", lines=["Face the camera and look at the next dot."]
            )
            self._pump(1.2)
        raise RuntimeError(f"{display.short} {name}: {why} after {self.s.retries + 1} tries")

    def run(self, displays: list[LogicalDisplay], zones: int) -> C.Calibration:
        self.wait_for_face(displays[0])
        out = []
        for d in displays:
            self._show(d.rect)
            self.ov.set_scene(
                title=f"Calibrating {d.short}",
                lines=[
                    "Look at each dot as it appears. Move your head naturally.",
                    self.cancel_hint,
                ],
            )
            self._pump(1.5)
            pts = [
                self.collect(d, i, name, fx, fy, len(C.POINTS))
                for i, (name, fx, fy) in enumerate(C.POINTS, 1)
            ]
            out.append(C.DisplayCalibration(d.id, d.label, d.rect, pts))
        self.ov.set_scene(title="Calibration done", lines=["Results are in the terminal."])
        self._pump(0.8)
        camera = max(self.frames, key=self.frames.get) if self.frames else None
        return C.Calibration(out, zones=zones, camera=camera if camera and camera[0] else None)


# Centres closer than this many times the per-dot noise can't be switched between reliably.
MIN_SEPARATION = 6.0


def separability(cal: C.Calibration, source: str) -> tuple[list[str], list[str]]:
    """(problems, report lines) about how well displays can be told apart by head pose.

    Problems: a display's *centre* dot, left out of the calibration, would be classified onto another
    display, or lies inside another display's calibrated region. Edge dots of adjacent displays are close
    by design (they straddle the bezel), so they aren't tested. The report gives centre-to-centre distances
    in pose units and in multiples of the calibration noise.
    """
    labels = {d.id: d.label.split(" (")[0] for d in cal.displays}
    problems: list[str] = []
    full = Classifier.from_calibration(cal, source)
    centres = {}
    for d in cal.displays:
        c = next((p for p in d.points if p.name == "center" and source in p.pose), None)
        if c is None:
            continue
        centres[d.id] = c.pose[source]
        others = [
            C.DisplayCalibration(
                x.id, x.label, x.rect, [q for q in x.points if not (x.id == d.id and q is c)]
            )
            for x in cal.displays
        ]
        got = Classifier.from_calibration(C.Calibration(others, zones=cal.zones), source).classify(
            *c.pose[source]
        )
        if got.display != d.id:
            problems.append(
                f"the centre of {labels[d.id]} looks like {labels.get(got.display, 'no display')}"
            )
        p = np.array(c.pose[source])
        for other, reg in full.regions.items():
            if other != d.id and reg.ok and reg.outside(p, full.scale) == 0.0:
                problems.append(f"the centre of {labels[d.id]} lies inside {labels[other]}")
    spreads = [
        float(np.hypot(*q.spread[source])) for x in cal.displays for q in x.points if source in q.spread
    ]
    noise = max(float(np.median(spreads)) if spreads else 0.0, 1e-6)
    report = []
    ids = list(centres)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            dist = float(np.hypot(centres[a][0] - centres[b][0], centres[a][1] - centres[b][1]))
            report.append(
                f"{labels[a]} <-> {labels[b]}: centres {dist:.1f} apart = {dist / noise:.0f}x the noise"
            )
            if dist < MIN_SEPARATION * noise:
                problems.append(
                    f"{labels[a]} and {labels[b]} are only {dist / noise:.1f}x the calibration noise apart"
                    f" (want {MIN_SEPARATION:g}x or more)"
                )
    return sorted(set(problems)), report
