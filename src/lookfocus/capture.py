"""Camera opening with timeout and backend fallback, plus frame sanity checks.

Frames live in memory only: nothing here writes an image anywhere or logs pixel data.

Opening is the slow, flaky part on Windows: MSMF can take seconds on some cameras, and DSHOW has had
crash reports after some Windows updates. open_camera() runs VideoCapture() on a worker thread and
gives up after a timeout; a late-finishing open is released by the worker itself so the device is not
leaked. Resolution is passed as open params (applied once during MSMF media-type negotiation) rather
than as a series of cap.set() calls, each of which can make MSMF renegotiate.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# lookfocus/__init__.py (always imported before this module) sets
# OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0 before cv2 is loaded.
import cv2
import numpy as np

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


def open_with_fallback(index: int, backend: str, *, fallback: bool = True, **kw) -> list[OpenResult]:
    """Try `backend`, then the other one if allowed. Returns every attempt; the last is the outcome."""
    attempts = [open_camera(index, backend, **kw)]
    if not attempts[0].ok and fallback:
        other = next(b for b in BACKENDS if b != backend)
        attempts.append(open_camera(index, other, **kw))
    return attempts


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
