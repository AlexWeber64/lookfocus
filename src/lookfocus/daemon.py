"""The LookFocus background engine: camera -> head pose -> classifier -> decision -> focus.

Threads (all daemon threads, stopped by Daemon.stop(); every loop blocks on an event or a timeout, so
there are no busy loops):
- capture       CaptureThread: camera + face tracker, publishes PoseSample to a LatestSlot (stale poses
                are overwritten, never queued); also yields the camera to other apps (D-023).
- input         InputPoller: mouse / inferred-keyboard activity every 40 ms (no hooks, no key identities).
- foreground    ForegroundTracker: last active window per logical display (4 Hz).
- decision      this module's loop: classify each new pose, run DecisionMachine, hand "focus display X"
                to the executor. Also re-checks the display layout every few seconds.
- focus         FocusExecutor: one-slot latest-request queue; picks the target window and runs the
                learned activation ladder.
UI (tray, hotkey, lock/sleep) attaches through on_status / on_event and set_paused (M5).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from lookfocus import monitors
from lookfocus.calibration import Calibration
from lookfocus.capture import CaptureSettings, CaptureThread, LatestSlot
from lookfocus.classifier import Classifier
from lookfocus.decision import NONE, DecisionMachine, DecisionSettings, Observation
from lookfocus.displays import LogicalDisplay, build_displays
from lookfocus.inputactivity import InputPoller
from lookfocus.pose import PoseSample
from lookfocus.targeting import StageLearner, display_of

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DaemonSettings:
    zones: int = 0
    pose_source: str = "matrix"
    none_margin: float = 6.0
    decision: DecisionSettings = field(default_factory=DecisionSettings)
    capture: CaptureSettings = field(default_factory=CaptureSettings)
    move_cursor_on_switch: bool = False
    allow_alt: bool = True
    dry_run: bool = False  # decide and report, but never move focus
    layout_check_s: float = 5.0


StatusCallback = Callable[[str, str], None]  # (status, detail)
EventCallback = Callable[[str, dict], None]  # (kind, info)

# Capture statuses -> what the app is doing, for the tray icon (M5).
_CAMERA_BUSY = {"opening", "error", "yielded", "stopped"}


class FocusExecutor(threading.Thread):
    """Runs focus requests one at a time; a newer request replaces one that hasn't started yet."""

    def __init__(self, daemon: Daemon) -> None:
        super().__init__(name="focus", daemon=True)
        self.d = daemon
        self.learner = StageLearner()
        self._lock = threading.Lock()
        self._request: str | None = None
        self._wake = threading.Event()
        self._halt = threading.Event()

    def request(self, display_id: str) -> None:
        with self._lock:
            self._request = display_id
        self._wake.set()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        self.join(5)

    def run(self) -> None:
        while not self._halt.is_set():
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                display_id, self._request = self._request, None
            if display_id is not None and not self._halt.is_set():
                try:
                    self._focus(display_id)
                except Exception:
                    log.exception("focus request failed")

    def _focus(self, display_id: str) -> None:
        from lookfocus import windows_focus as wf

        d = self.d
        display = d.display_by_id.get(display_id)
        if display is None:
            return
        target = d.tracker.target_for(display)
        if target is None:
            log.info("facing %s: no window there to focus", display.short)
            d.emit("no_window", display=display.label)
            return
        if d.settings.dry_run:
            log.info("[dry run] would focus %s on %s", target.label, display.short)
            d.emit("switched", display=display.label, window=target.label, stage="dry-run", ms=0.0)
            return
        key = target.cls
        t0 = time.monotonic()
        d.input.ignore_for(3.0)  # the ladder may inject a key tap; don't read it as typing
        act = wf.focus_window(
            target.hwnd, allow_alt=d.settings.allow_alt, first_stage=self.learner.first_stage(key)
        )
        d.input.ignore_between(t0 - 0.05, time.monotonic() + 0.1)
        self.learner.record(key, act)
        if act.ok:
            d.tracker.note(target.hwnd)
            if d.settings.move_cursor_on_switch and act.stage != "already":
                wf.move_cursor_into(display.rect)
                d.input.reset_cursor()
            d.emit("switched", display=display.label, window=target.label, stage=act.stage, ms=act.total_ms)
        else:
            d.emit("focus_failed", display=display.label, window=target.label, reason=act.reason)


class Daemon:
    def __init__(
        self,
        settings: DaemonSettings,
        calibration: Calibration,
        *,
        on_status: StatusCallback | None = None,
        on_event: EventCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        from lookfocus.windows_focus import ForegroundTracker

        self.settings = settings
        self.calibration = calibration
        self.on_status = on_status
        self.on_event = on_event
        self.clock = clock
        self.classifier = Classifier.from_calibration(calibration, settings.pose_source, settings.none_margin)
        self.machine = DecisionMachine(settings.decision)
        self.displays: list[LogicalDisplay] = []
        self.display_by_id: dict[str, LogicalDisplay] = {}
        self.layout_ok = True
        self.poses: LatestSlot[PoseSample] = LatestSlot()
        self.capture = CaptureThread(settings.capture, self.poses, on_event=self._capture_event)
        self.input = InputPoller(clock=clock)
        self.tracker = ForegroundTracker(lambda: self.displays)
        self.executor = FocusExecutor(self)
        self._paused = False
        self._frame: tuple[int, int] = (0, 0)
        self._halt = threading.Event()
        self._thread = threading.Thread(target=self._decision_loop, name="decision", daemon=True)
        self._status: tuple[str, str] = ("starting", "")

    # --- lifecycle / control --------------------------------------------------------------------
    def start(self) -> None:
        self._refresh_layout()
        self._seed_from_foreground()
        for t in (self.input, self.tracker, self.executor, self.capture, self._thread):
            t.start()

    def stop(self) -> None:
        self._halt.set()
        self._thread.join(3)
        self.capture.stop()
        for t in (self.executor, self.tracker, self.input):
            t.stop()

    def set_paused(self, paused: bool) -> None:
        """Paused: no switching and the camera is released (LED off)."""
        self._paused = paused
        self.capture.set_paused(paused)
        if not paused:
            self._seed_from_foreground()  # don't jump on resume; switch when the user next looks elsewhere

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def status(self) -> tuple[str, str]:
        return self._status

    def emit(self, kind: str, **info) -> None:
        log.info("event %s %s", kind, info)
        if self.on_event is not None:
            try:
                self.on_event(kind, info)
            except Exception:
                log.exception("event handler failed")

    # --- internals ------------------------------------------------------------------------------
    def _capture_event(self, kind: str, info: dict) -> None:
        self.emit(f"camera_{kind}", **info)

    def _set_status(self, status: str, detail: str = "") -> None:
        if (status, detail) != self._status:
            self._status = (status, detail)
            log.info("status: %s %s", status, detail)
            if self.on_status is not None:
                try:
                    self.on_status(status, detail)
                except Exception:
                    log.exception("status handler failed")

    def _refresh_layout(self) -> None:
        ds = build_displays(monitors.list_monitors(), self.settings.zones)
        missing, added = self.calibration.check_layout(ds)
        ok = not missing and not added
        if [d.id for d in ds] != [d.id for d in self.displays]:
            self.displays = ds
            self.display_by_id = {d.id: d for d in ds}
        if ok != self.layout_ok:
            self.layout_ok = ok
            if not ok:
                log.warning("display layout changed (missing %s, new %s): recalibrate", missing, added)
                self.emit("layout_changed", missing=missing, added=added)
            else:
                log.info("display layout matches the calibration again")

    def _seed_from_foreground(self) -> None:
        from lookfocus import windows_focus as wf

        try:
            fg = wf.root_owner(wf.GetForegroundWindow() or 0)
            d = display_of(wf.window_info(fg), self.displays) if fg and wf.is_candidate(fg) else None
        except Exception:
            d = None
        self.machine.seed(d.id if d else None)

    def _decision_loop(self) -> None:
        seen = 0
        next_layout = self.clock() + self.settings.layout_check_s
        src = self.settings.pose_source
        while not self._halt.is_set():
            seq, sample = self.poses.wait_newer(seen, 0.1)
            fresh, seen = seq != seen, seq
            now = self.clock()
            if now >= next_layout:
                next_layout = now + self.settings.layout_check_s
                try:
                    self._refresh_layout()
                except Exception:
                    log.exception("layout check failed")
            if fresh and sample is not None and sample.frame != self._frame:
                self._frame = sample.frame
                if self.calibration.camera_mismatch(sample.frame):
                    cw, ch = self.calibration.camera
                    log.warning("camera mode %dx%d differs from calibration (%dx%d)", *sample.frame, cw, ch)
                    self.emit("camera_mode_changed", now=sample.frame, calibrated=self.calibration.camera)
            display, distances = None, {}  # a timeout with no new frame counts as a gap, never as a pose
            if fresh and sample is not None and sample.pose is not None and sample.status == "tracking":
                r = self.classifier.classify(*sample.pose.features(src))
                display, distances = (r.display or NONE), r.distance
            inp = self.input.state()
            obs = Observation(
                display, distances, inp.mouse_active_at, inp.keyboard_at, self._paused or not self.layout_ok
            )
            target = self.machine.update(now, obs)
            if target is not None:
                log.info(
                    "facing %s", self.display_by_id[target].short if target in self.display_by_id else target
                )
                self.executor.request(target)
            self._set_status(*self._describe(sample))

    def _describe(self, sample: PoseSample | None) -> tuple[str, str]:
        if self._paused:
            return "paused", ""
        if not self.layout_ok:
            return "layout_changed", "display layout changed: recalibrate"
        if sample is None or sample.status in _CAMERA_BUSY:
            default = {"opening": "opening camera", "stopped": "camera stopped"}
            return "camera", (
                sample.message or default.get(sample.status, sample.status)
            ) if sample else "starting"
        if sample.status == "no_face":
            return "no_face", sample.message
        cur = self.display_by_id.get(self.machine.current or "")
        return "active", cur.short if cur else ""
