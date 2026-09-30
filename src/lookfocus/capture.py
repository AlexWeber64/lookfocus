"""Camera capture + face-tracking thread.

Frames live in memory only: nothing here writes an image anywhere or logs pixel data.

Opening is the slow, flaky part on Windows: MSMF can take seconds on some cameras, and DSHOW has had
crash reports after some Windows updates. open_camera() runs VideoCapture() on a worker thread and
gives up after a timeout; a late-finishing open is released by the worker itself so the device is not
leaked. Resolution is passed as open params (applied once during MSMF media-type negotiation) rather
than as a series of cap.set() calls, each of which can make MSMF renegotiate.

CaptureThread owns camera + tracker and publishes one PoseSample per processed frame into a LatestSlot
(single value, overwritten: consumers always see the newest pose and stale ones are dropped, never
queued).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# lookfocus/__init__.py (always imported before this module) sets
# OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0 before cv2 is loaded.
import cv2
import numpy as np

from lookfocus import camera_sharing, privacy
from lookfocus import pose as posemath
from lookfocus.camera_activity import SensorActivityMonitor, SensorDevice, process_name
from lookfocus.camera_enum import CameraChoice, candidate_indices, device_key, list_cameras
from lookfocus.camera_sharing import Sharing
from lookfocus.camera_usage import ActivityPolicy, Others, busy_notice, friendly_app_name, yield_notice
from lookfocus.paths import MODEL_PATH

log = logging.getLogger(__name__)

BACKENDS: dict[str, int] = {"msmf": cv2.CAP_MSMF, "dshow": cv2.CAP_DSHOW}


@dataclass
class OpenResult:
    cap: cv2.VideoCapture | None
    backend: str
    index: int
    open_s: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.cap is not None


def open_params(width: int | None, height: int | None, fps: float | None) -> list[int]:
    params: list[int] = []
    if width:
        params += [cv2.CAP_PROP_FRAME_WIDTH, int(width)]
    if height:
        params += [cv2.CAP_PROP_FRAME_HEIGHT, int(height)]
    if fps:
        params += [cv2.CAP_PROP_FPS, int(fps)]
    return params


def open_camera(
    index: int,
    backend: str,
    *,
    width: int | None = 640,
    height: int | None = 480,
    fps: float | None = None,
    timeout_s: float = 8.0,
) -> OpenResult:
    """Open camera `index` with `backend` ("msmf"/"dshow"); never blocks longer than timeout_s."""
    api = BACKENDS[backend]
    params = open_params(width, height, fps)
    lock = threading.Lock()
    state: dict = {"abandoned": False}

    def worker() -> None:
        t0 = time.perf_counter()
        try:
            cap = cv2.VideoCapture(index, api, params) if params else cv2.VideoCapture(index, api)
            err = "" if cap.isOpened() else "VideoCapture did not open"
        except Exception as e:  # cv2 raises cv2.error on some driver failures
            cap, err = None, f"{type(e).__name__}: {e}"
        dt = time.perf_counter() - t0
        with lock:
            if state["abandoned"]:
                if cap is not None:
                    cap.release()
                return
            state.update(cap=cap, err=err, dt=dt)

    t0 = time.perf_counter()
    th = threading.Thread(target=worker, name=f"cam-open-{backend}-{index}", daemon=True)
    th.start()
    th.join(timeout_s)
    with lock:
        if "dt" not in state:
            state["abandoned"] = True
            return OpenResult(
                None, backend, index, time.perf_counter() - t0, f"timed out after {timeout_s:.1f}s"
            )
    cap, err, dt = state["cap"], state["err"], state["dt"]
    if err:
        if cap is not None:
            cap.release()
        return OpenResult(None, backend, index, dt, err)
    return OpenResult(cap, backend, index, dt)


@dataclass(frozen=True)
class FrameStats:
    mean_luma: float
    chroma: float  # mean |B-G| + |G-R|; ~0 for greyscale/IR sensors
    greyscale: bool
    dark: bool


GREY_CHROMA = 1.5
DARK_LUMA = 12.0


def frame_stats(frame: np.ndarray) -> FrameStats:
    """Cheap colour/brightness summary on a subsampled view of the frame."""
    small = frame[::8, ::8]
    if small.ndim == 2 or small.shape[2] == 1:
        luma = float(small.mean())
        return FrameStats(luma, 0.0, True, luma < DARK_LUMA)
    f = small.astype(np.int16)
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    chroma = float(np.abs(b - g).mean() + np.abs(g - r).mean())
    luma = float((0.114 * b + 0.587 * g + 0.299 * r).mean())
    dark = luma < DARK_LUMA
    return FrameStats(luma, chroma, (chroma < GREY_CHROMA) and not dark, dark)


# A live sensor always shows some frame-to-frame noise; a synthetic placeholder image shows none. With
# this ThinkPad's software camera shutter on, frames keep coming at 30 fps but are flat grey (luma ~140,
# chroma ~4, vs ~35 live), so a placeholder is suspected; the dev camera in a dark room still measured
# motion ~0.3.
STATIC_MOTION = 0.25


def thumbnail(frame: np.ndarray) -> np.ndarray:
    """Tiny greyscale copy for frame-to-frame comparison."""
    small = frame[::8, ::8]
    if small.ndim == 3:
        small = small.mean(axis=2)
    return small.astype(np.float32)


def motion(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute difference between two thumbnails (0 = identical frames)."""
    return float(np.abs(a - b).mean())


# --------------------------------------------------------------------------------------------------
# Camera source: device selection, verification, fresh-frame reads
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureSettings:
    backend: str = "msmf"
    fallback: bool = True  # try the other backend if nothing opens on `backend`
    camera: CameraChoice = field(default_factory=CameraChoice)
    width: int = 640
    height: int = 480
    fps: float = 15.0  # processing rate while a face is (recently) visible
    idle_fps: float = 2.5  # processing rate after no_face_s without a face
    no_face_s: float = 3.0
    open_timeout_s: float = 8.0
    first_frames_timeout_s: float = 3.0  # an open that yields no frames by then counts as failed
    stall_s: float = 3.0  # no frame for this long while running -> reopen
    backoff_s: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)
    yield_to_other_apps: bool = True  # release the camera when another app starts using one (D-015)
    probe_sharing: bool = True  # after each open, check whether a second app could use the camera too
    yield_grace_s: float = 2.0  # resume this long after the other app stopped


class CameraError(RuntimeError):
    pass


# Reads that return faster than this came out of the backend's buffer rather than from the sensor.
# Measured on the dev camera: buffered reads take 0.2-3 ms, sensor-paced ones 20-70 ms (D-012).
FRESH_READ_S = 0.008
MAX_DRAIN = 4
NO_FRAMES = "opened but delivered no frames"
# A camera held by another app can still "open" and trickle out a frame a second (DSHOW, measured), so an
# open only counts once this many real frames arrive within first_frames_timeout_s (a free camera does
# that in well under half a second).
VERIFY_FRAMES = 5
# KSCATEGORY_SENSOR_CAMERA: the IR / Windows Hello sensor published by the same camera module. Its
# symbolic link shares the device instance with the colour camera, so device_key() can't separate them.
SENSOR_CAMERA_CATEGORY = "{24e552d7-6523-47f7-a647-d3465bf1f5ca}"


class CameraSource:
    """Owns one opened VideoCapture."""

    def __init__(self, settings: CaptureSettings) -> None:
        self.settings = settings
        self.cap: cv2.VideoCapture | None = None
        self.description = ""
        self.backend = ""
        self.index = -1
        self.name = ""
        self.path = ""
        self.busy = False  # last open failed only because no device delivered frames
        self.tried_keys: set[str] = set()  # device keys the last open() attempted (for busy detection)

    def open(self) -> None:
        """Pick, open and verify a camera, or raise CameraError explaining every attempt."""
        s = self.settings
        backends = [s.backend] + ([b for b in BACKENDS if b != s.backend] if s.fallback else [])
        problems: list[str] = []
        opened_without_frames = 0
        other_failures = 0
        self.tried_keys = set()
        for backend in backends:
            devices = list_cameras(backend)
            by_index = {d.index: d for d in devices}
            indices, explicit = candidate_indices(devices, s.camera)
            if (s.camera.path or s.camera.name) and not explicit:
                problems.append(f"{backend}: configured camera not found, trying others")
            for idx in indices:
                dev = by_index.get(idx)
                if dev is not None and dev.path:
                    self.tried_keys.add(device_key(dev.path))
                label = f"{backend}:{idx} {dev.name if dev else '?'!r}"
                res = open_camera(idx, backend, width=s.width, height=s.height, timeout_s=s.open_timeout_s)
                if not res.ok:
                    problems.append(f"{label}: {res.error}")
                    other_failures += 1
                    continue
                verdict = self._verify(res.cap)
                if verdict == "ok":
                    self.cap = res.cap
                    self.description = label
                    self.backend, self.index = backend, idx
                    self.name, self.path = (dev.name, dev.path) if dev else ("", "")
                    self.busy = False
                    log.info("camera open: %s (%.2fs)", label, res.open_s)
                    return
                res.cap.release()
                problems.append(f"{label}: {verdict}")
                if verdict.startswith(NO_FRAMES):
                    opened_without_frames += 1
                else:
                    other_failures += 1
                log.info("camera rejected: %s: %s", label, verdict)
        # Some device opened but gave no frames: most likely another app holds it (other devices failing
        # for other reasons, e.g. an OBS virtual camera with OBS closed, don't change that).
        self.busy = opened_without_frames > 0
        raise CameraError("; ".join(problems) or "no cameras found")

    def _verify(self, cap: cv2.VideoCapture) -> str:
        """A steady stream of real frames must arrive, and not from a greyscale (IR) sensor."""
        t_end = time.monotonic() + self.settings.first_frames_timeout_s
        stats: list[FrameStats] = []
        prev = None
        while time.monotonic() < t_end and len(stats) < VERIFY_FRAMES:
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.02)
                continue
            th = thumbnail(frame)
            # blocked clients may get all-black frames; a live sensor is lit or at least noisy
            if float(th.mean()) > 0.5 or (prev is not None and motion(prev, th) > 0.05):
                stats.append(frame_stats(frame))
            prev = th
        if len(stats) < VERIFY_FRAMES:
            return NO_FRAMES + " (camera in use by another app?)"
        if sum(st.greyscale for st in stats) * 2 > len(stats):
            return "greyscale frames (IR / Windows Hello sensor?), skipped"
        return "ok"

    def read_fresh(self) -> np.ndarray | None:
        """Drop frames the backend already buffered (possibly old), then return the next one."""
        if self.cap is None:
            return None
        for _ in range(MAX_DRAIN):
            t = time.perf_counter()
            if not self.cap.grab():
                return None
            if time.perf_counter() - t > FRESH_READ_S:
                break
        ok, frame = self.cap.retrieve()
        return frame if ok else None

    def release(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


# --------------------------------------------------------------------------------------------------
# Face tracker (MediaPipe Face Landmarker)
# --------------------------------------------------------------------------------------------------


class FaceTracker:
    def __init__(self, model_path=MODEL_PATH) -> None:
        privacy.verify_runtime(model_path)  # before mediapipe's native library is loaded
        import mediapipe as mp

        self._mp = mp
        opts = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_faces=1,
            output_facial_transformation_matrixes=True,
        )
        self._lm = mp.tasks.vision.FaceLandmarker.create_from_options(opts)
        self._last_ts = -1

    def process(self, frame_bgr: np.ndarray, t: float) -> tuple[posemath.HeadPose | None, np.ndarray | None]:
        """(pose, normalized landmarks Nx3) for the most prominent face, or (None, None)."""
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        ts = max(int(t * 1000), self._last_ts + 1)  # VIDEO mode needs strictly increasing timestamps
        self._last_ts = ts
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        res = self._lm.detect_for_video(image, ts)
        if not res.face_landmarks:
            return None, None
        pts = np.array([(p.x, p.y, p.z) for p in res.face_landmarks[0]], dtype=np.float64)
        mats = res.facial_transformation_matrixes
        matrix = np.asarray(mats[0], dtype=np.float64) if mats else None
        h, w = frame_bgr.shape[:2]
        return posemath.head_pose(matrix, pts, w, h), pts

    def close(self) -> None:
        self._lm.close()


# --------------------------------------------------------------------------------------------------
# Latest-value slot and the capture thread
# --------------------------------------------------------------------------------------------------


class LatestSlot[T]:
    """Holds only the newest value. Writers overwrite; readers wait for something newer than they saw."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._value: T | None = None
        self._seq = 0

    def put(self, value: T) -> None:
        with self._cond:
            self._value = value
            self._seq += 1
            self._cond.notify_all()

    def get(self) -> tuple[int, T | None]:
        with self._cond:
            return self._seq, self._value

    def wait_newer(self, seen: int, timeout: float) -> tuple[int, T | None]:
        with self._cond:
            self._cond.wait_for(lambda: self._seq != seen, timeout)
            return self._seq, self._value


DebugHook = Callable[[np.ndarray, "np.ndarray | None", posemath.PoseSample], None]


class CaptureThread(threading.Thread):
    """Camera -> FaceTracker -> LatestSlot[PoseSample], with reopen/backoff and a low-rate no-face mode.

    Statuses: opening, tracking (face in this frame), lost (no face, but seen within no_face_s),
    no_face (none for no_face_s: processing drops to idle_fps), error, paused (camera released),
    yielded (camera released for another app, D-015/D-023), stopped.

    Events, via `on_event(kind, info)` from this thread: "yielded" / "busy" (with a notification title and
    body), "resumed", "sharing" (result of the camera-sharing probe).
    """

    def __init__(
        self,
        settings: CaptureSettings,
        slot: LatestSlot[posemath.PoseSample],
        *,
        debug_hook: DebugHook | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="capture", daemon=True)
        self.settings = settings
        self.slot = slot
        self.debug_hook = debug_hook
        self.on_event = on_event
        self.clock = clock
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self._paused = False
        self.sharing = Sharing.UNKNOWN
        self.sharing_available = False
        self._policy = ActivityPolicy(grace_s=settings.yield_grace_s)
        self._activity_lock = threading.Lock()
        self._activity: list[SensorDevice] = []
        self._own_pids = {os.getpid()}  # this process and its camera-sharing probe children
        self._pid_names: dict[int, str] = {}  # looked up on first sight: a process may exit before we report
        self._force_resume = False
        self._yield_message = ""
        self._gen = 0  # bumped whenever the camera is opened or released; stale probe results are dropped
        self._probe_pending = False
        self._sharing_went_off = False

    # --- control (any thread) ---
    def stop(self, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        self._wake.set()
        self.join(timeout)

    def set_paused(self, paused: bool) -> None:
        """Paused = camera released (LED off) and no processing, until resumed."""
        self._paused = paused
        self._wake.set()

    def resume_from_yield(self) -> None:
        """User override: take the camera back even though another app still reports using one."""
        with self._activity_lock:
            self._force_resume = True
        self._wake.set()

    @property
    def yielded(self) -> bool:
        return self._policy.yielded

    # --- thread body ---
    def _publish(self, status: str, **kw) -> None:
        self.slot.put(posemath.PoseSample(t=self.clock(), status=status, **kw))

    def _emit(self, kind: str, **info) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, info)
            except Exception:
                log.exception("capture event handler failed")

    def _sleep(self, seconds: float) -> None:
        """Interruptible by stop()/set_paused()/other-app events."""
        if seconds > 0:
            self._wake.wait(seconds)
        self._wake.clear()

    def _on_activity(self, devices: list[SensorDevice]) -> None:
        """MF sensor activity monitor (MF worker thread): camera usage changed somewhere."""
        with self._activity_lock:
            self._activity = devices
        self._wake.set()

    def _others(self, source: CameraSource) -> Others:
        """Processes other than LookFocus that have LookFocus's camera open: pid -> streaming."""
        keys = {device_key(source.path)} if source.path else set(source.tried_keys)
        with self._activity_lock:
            devices = list(self._activity)
            own = set(self._own_pids)
        out: Others = {}
        for d in devices:
            if SENSOR_CAMERA_CATEGORY in d.link.lower():
                continue  # IR / Windows Hello stream of the same module: not the camera LookFocus uses
            if keys and device_key(d.link) not in keys:
                continue
            for p in d.processes:
                if p.pid not in own:
                    out[p.pid] = out.get(p.pid, False) or p.streaming
                    if p.pid not in self._pid_names:
                        self._pid_names[p.pid] = friendly_app_name(process_name(p.pid))
        return out

    def _names(self, pids) -> list[str]:
        return [self._pid_names.get(p) or friendly_app_name("") for p in sorted(pids)]

    def run(self) -> None:
        try:
            tracker = FaceTracker()
        except Exception as e:  # privacy check failure, missing model, broken mediapipe install
            log.error("face tracker unavailable: %s", e)
            self._publish("error", message=f"face tracker unavailable: {e}")
            return
        try:
            self.sharing_available = camera_sharing.feature_available(camera_sharing.windows_build())
        except OSError:
            self.sharing_available = False
        if not self.sharing_available:
            self.sharing = Sharing.UNSUPPORTED
        monitor = None
        if self.settings.yield_to_other_apps:
            monitor = SensorActivityMonitor(self._on_activity)
            monitor.start()
            monitor.started_ok.wait(5)
            if monitor.error:
                log.warning("can't watch other apps' camera use: %s", monitor.error)
        source = CameraSource(self.settings)
        try:
            self._loop(source, tracker)
        finally:
            if monitor is not None:
                monitor.stop()
            source.release()
            tracker.close()
            self._publish("stopped")

    def _probe_sharing(self, gen: int, backend: str, index: int, path: str) -> None:
        def own(pid: int) -> None:
            with self._activity_lock:
                self._own_pids.add(pid)

        try:
            state = camera_sharing.probe_while_holding(backend, index, on_child_pid=own)
            hint = camera_sharing.frame_server_hint(path) if path else None
            if gen != self._gen:
                log.info("camera sharing probe discarded: camera was released while it ran")
                return
            if state is not Sharing.UNKNOWN:
                if state is Sharing.OFF and self.sharing is Sharing.ON:
                    self._sharing_went_off = True  # applied on the capture thread (policy isn't locked)
                self.sharing = state
            log.info("camera sharing: %s (probe), MEPSGFlag hint=%s", state.value, hint)
            self._emit("sharing", state=state.value, hint=hint, available=self.sharing_available)
        finally:
            self._probe_pending = False
            self._wake.set()  # re-evaluate any app that opened the camera while we didn't know yet

    def _handle_activity(self, source: CameraSource) -> None:
        """Apply ActivityPolicy to the latest camera activity report."""
        with self._activity_lock:
            force, self._force_resume = self._force_resume, False
        policy = self._policy
        others = self._others(source)
        if self._sharing_went_off:
            self._sharing_went_off = False
            policy.sharing_turned_off(others)
        if force and policy.yielded:
            policy.user_resume(others)
            log.info("camera taken back by the user")
            self._emit("resumed", manual=True, apps=[])
            return
        if policy.yielded:
            log.debug("yielded; other processes on the camera (pid: streaming): %s", others)
        elif self._probe_pending and self.sharing is Sharing.UNKNOWN and source.cap is not None:
            # First probe of this run still going: don't guess "off" for an app that opens the camera now
            # (measured race, D-026). It's re-evaluated the moment the probe finishes.
            return
        blockers_before = set(policy.blockers)
        action = policy.update(self.clock(), others, source.cap is not None, self.sharing is Sharing.ON)
        if action == "yield":
            apps = self._names(policy.blockers)
            source.release()
            self._gen += 1
            title, body = yield_notice(apps, self.sharing_available, source.name)
            self._yield_message = f"camera released for {', '.join(apps)}; resumes when it's done"
            log.info("%s opened the camera; released it (sharing: %s)", apps, self.sharing.value)
            self._emit("yielded", apps=apps, title=title, body=body)
        elif action == "resume":
            apps = self._names(blockers_before)
            log.info("%s done with the camera; resuming", apps)
            self._emit("resumed", manual=False, apps=apps)

    def _loop(self, source: CameraSource, tracker: FaceTracker) -> None:
        s = self.settings
        attempt = 0
        last_face = last_frame = self.clock()
        next_due = 0.0
        fps_ema = 0.0
        last_processed: float | None = None
        prev_thumb: np.ndarray | None = None
        static_run = 0

        while not self._stop_evt.is_set():
            if self._paused:
                if source.cap is not None:
                    source.release()
                    self._gen += 1
                    log.info("paused: camera released")
                self._publish("paused")
                while self._paused and not self._stop_evt.is_set():
                    self._sleep(3600)
                continue

            if s.yield_to_other_apps:
                self._handle_activity(source)
                if self._policy.yielded:
                    self._publish("yielded", message=self._yield_message)
                    self._sleep(0.5)  # woken early by activity reports; the timeout drives the grace timers
                    continue

            if source.cap is None:
                self._publish("opening")
                try:
                    source.open()
                except CameraError as e:
                    if s.yield_to_other_apps and source.busy and self._wait_for_busy_camera(source):
                        continue
                    wait = s.backoff_s[min(attempt, len(s.backoff_s) - 1)]
                    attempt += 1
                    log.warning("camera unavailable (retry in %.0fs): %s", wait, e)
                    self._publish("error", message=f"{e} (retrying in {wait:.0f}s)")
                    self._sleep(wait)
                    continue
                attempt = 0
                last_face = last_frame = self.clock()
                next_due = 0.0
                last_processed, prev_thumb, static_run = None, None, 0
                self._gen += 1
                self._policy.start_holding(self._others(source))
                if s.probe_sharing and self.sharing_available:
                    self._probe_pending = True
                    threading.Thread(
                        target=self._probe_sharing,
                        args=(self._gen, source.backend, source.index, source.path),
                        name="sharing-probe",
                        daemon=True,
                    ).start()

            now = self.clock()
            if next_due > now:
                self._sleep(next_due - now)
                if self._stop_evt.is_set() or self._paused:
                    continue

            frame = source.read_fresh()
            t = self.clock()
            if frame is None:
                if t - last_frame > s.stall_s:
                    log.warning("camera stopped delivering frames; reopening")
                    source.release()
                    self._gen += 1
                    self._publish("error", message="camera stopped delivering frames; reopening")
                continue
            last_frame = t

            t0 = time.perf_counter()
            try:
                head, pts = tracker.process(frame, t)
            except Exception as e:  # keep the thread alive; report and carry on
                log.exception("face tracking failed")
                self._publish("error", message=f"face tracking failed: {e}")
                self._sleep(1.0)
                continue
            infer_ms = (time.perf_counter() - t0) * 1000

            if last_processed is not None:
                inst = 1.0 / max(t - last_processed, 1e-3)
                fps_ema = inst if fps_ema == 0 else 0.8 * fps_ema + 0.2 * inst
            last_processed = t

            message = ""
            if head is not None:
                last_face = t
                status = "tracking"
                prev_thumb, static_run = None, 0
            else:
                status = "no_face" if t - last_face >= s.no_face_s else "lost"
                message, prev_thumb, static_run = _feed_hint(frame, prev_thumb, static_run)

            period = 1.0 / (s.idle_fps if status == "no_face" else s.fps)
            next_due = max(next_due + period, t) if next_due else t + period

            sample = posemath.PoseSample(
                t=t,
                status=status,
                pose=head,
                fps=fps_ema,
                infer_ms=infer_ms,
                camera=source.description,
                message=message,
            )
            self.slot.put(sample)
            if self.debug_hook is not None:
                self.debug_hook(frame, pts, sample)

    def _wait_for_busy_camera(self, source: CameraSource) -> bool:
        """Open got no frames: if other processes are streaming from this camera, wait for them."""
        time.sleep(0.3)  # let the activity report for our own failed open settle
        if not self._policy.block_on(self.clock(), self._others(source)):
            return False
        apps = self._names(self._policy.blockers)
        title, body = busy_notice(apps, self.sharing_available)
        self._yield_message = f"camera in use by {', '.join(apps)}; starting when it's done"
        log.info("camera busy (%s); waiting for it", apps)
        self._emit("busy", apps=apps, title=title, body=body)
        return True


def _feed_hint(frame: np.ndarray, prev: np.ndarray | None, static_run: int) -> tuple[str, np.ndarray, int]:
    """Explain a missing face when the image itself is the reason."""
    th = thumbnail(frame)
    static_run = static_run + 1 if prev is not None and motion(prev, th) < STATIC_MOTION else 0
    if frame_stats(frame).dark:
        return "camera image is dark (lens covered or lights off?)", th, static_run
    if static_run >= 3:
        return "camera image is frozen (software privacy shutter on?)", th, static_run
    return "", th, static_run
