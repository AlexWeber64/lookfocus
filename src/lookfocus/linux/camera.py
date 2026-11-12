"""Cameras on Linux: PipeWire (the default) and plain V4L2.

PipeWire is how KDE Plasma and the browsers reach cameras today, and on laptops with MIPI sensors (Intel
IPU6/IPU7, like the dev machine's) it is the *only* way: the /dev/video* nodes carry raw sensor data and
libcamera's ISP runs behind PipeWire. PipeWire also hands the same camera to several apps at once, so a
video call and LookFocus simply share it (no yielding needed, unlike Windows without its multi-app setting).

OpenCV's pip wheels have no PipeWire or GStreamer support, so frames come from a `gst-launch-1.0`
child process (pipewiresrc -> rate limit -> convert/scale to BGR) through a pipe: memory only, never a
file. GstCapture wraps that in the few cv2.VideoCapture methods capture.CameraSource uses. The child dies
with LookFocus: it runs under a tiny shell that kills it when its stdin (held by LookFocus) closes.

"v4l2" opens /dev/videoN through OpenCV's V4L2 backend, for USB webcams on systems without PipeWire.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from lookfocus.camera_enum import CameraDevice

log = logging.getLogger(__name__)

PIPEWIRE = -1  # pseudo backend id in capture.BACKENDS (cv2 has none for PipeWire)


# --------------------------------------------------------------------------------------------------
# enumeration
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PipeWireCamera:
    serial: int
    node_name: str  # stable across restarts (e.g. "libcamera_input.__SB_.LNK0"); what pipewiresrc targets
    description: str
    # Sensor mounting rotation as libcamera reports it. Informational only: PipeWire's libcamera source
    # already delivers upright frames (measured on the IMX471, rotation 180: a face was found in 121 of 121
    # frames as delivered, 6 of 121 after flipping them again), so LookFocus never rotates.
    rotation: int


def pipewire_cameras_from(dump: list) -> list[PipeWireCamera]:
    """Pure: camera nodes in a `pw-dump` result (screen casts are Video/Source too: skipped by role/api)."""
    out = []
    for o in dump:
        if not str(o.get("type", "")).endswith(":Node"):
            continue
        p = (o.get("info") or {}).get("props") or {}
        if p.get("media.class") != "Video/Source":
            continue
        if p.get("media.role") != "Camera" and p.get("device.api") not in ("libcamera", "v4l2"):
            continue
        out.append(
            PipeWireCamera(
                serial=int(p.get("object.serial", o.get("id", 0))),
                node_name=str(p.get("node.name", "")),
                description=str(p.get("node.description") or p.get("node.nick") or p.get("node.name", "")),
                rotation=int(p.get("api.libcamera.rotation", 0) or 0),
            )
        )
    return sorted(out, key=lambda c: c.serial)


_pw_cache: list[PipeWireCamera] = []


def pipewire_cameras() -> list[PipeWireCamera]:
    global _pw_cache
    if shutil.which("pw-dump") is None:
        return []
    try:
        res = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5, check=False)
        _pw_cache = pipewire_cameras_from(json.loads(res.stdout or "[]"))
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        log.warning("pw-dump failed: %s", e)
    return _pw_cache


VIDIOC_QUERYCAP = 0x80685600
V4L2_CAP_VIDEO_CAPTURE, V4L2_CAP_DEVICE_CAPS = 0x00000001, 0x80000000


def _v4l2_is_capture(dev: Path) -> bool:
    """A real capture node (not metadata or a raw MIPI CSI node with no usable formats... best effort)."""
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
        caps, device_caps = struct.unpack_from("<II", buf, 84)
        effective = device_caps if caps & V4L2_CAP_DEVICE_CAPS else caps
        return bool(effective & V4L2_CAP_VIDEO_CAPTURE)
    except OSError:
        return False
    finally:
        os.close(fd)


def v4l2_cameras() -> list[CameraDevice]:
    out = []
    for d in sorted(Path("/sys/class/video4linux").glob("video*"), key=lambda p: int(p.name[5:] or 0)):
        dev = Path("/dev") / d.name
        try:
            name = (d / "name").read_text().strip()
        except OSError:
            name = d.name
        if "ISYS" in name or "CSI" in name:  # Intel IPU raw nodes: need libcamera (use PipeWire)
            continue
        if _v4l2_is_capture(dev):
            by_path = next((p for p in Path("/dev/v4l/by-path").glob("*") if p.resolve() == dev), None)
            out.append(CameraDevice("v4l2", int(d.name[5:]), name, str(by_path or dev)))
    return out


def list_cameras(backend: str) -> list[CameraDevice]:
    if backend == "pipewire":
        return [
            CameraDevice("pipewire", i, c.description, f"pipewire:{c.node_name}")
            for i, c in enumerate(pipewire_cameras())
        ]
    if backend == "v4l2":
        return v4l2_cameras()
    raise ValueError(f"unknown camera backend {backend!r}")


# --------------------------------------------------------------------------------------------------
# capture through gst-launch
# --------------------------------------------------------------------------------------------------

# Runs the pipeline with the frame pipe ($1) as its fd 3, and kills it when LookFocus goes away (its stdin
# closes) or asks it to (closes stdin itself). The shell drops its own copy of the pipe so a dead pipeline
# reads as end-of-file.
_SUPERVISOR = (
    'fd=$1; shift; trap "" PIPE; "$@" 3>&"$fd" & pid=$!; eval "exec $fd>&-"; '
    'read -r _; kill "$pid" 2>/dev/null; wait "$pid"'
)
_CAPS = re.compile(r"fdsink\d*\.GstPad:sink: caps = video/x-raw.*?width=\(int\)(\d+), height=\(int\)(\d+)")


class GstCapture:
    """A cv2.VideoCapture look-alike reading BGR frames from a gst-launch-1.0 PipeWire pipeline."""

    def __init__(self, node_name: str, width: int, fps: float) -> None:
        self.node_name = node_name
        self._proc: subprocess.Popen | None = None
        self._rfd = -1
        self._size: tuple[int, int] = (0, 0)
        self._sized = threading.Event()
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._taken = 0
        self._dead = False
        gst = shutil.which("gst-launch-1.0")
        if gst is None:
            raise RuntimeError("gst-launch-1.0 not found (install gstreamer1 and pipewire-gstreamer)")
        rate = max(1, round(fps))
        pipeline = [
            "pipewiresrc", f"target-object={node_name}", "do-timestamp=true", "!",
            "videorate", "drop-only=true", "!", f"video/x-raw,framerate={rate}/1", "!",
            "videoconvert", "!",
            "videoscale", "!",
            f"video/x-raw,format=BGR,width={int(width)},pixel-aspect-ratio=1/1", "!",
            "fdsink", "fd=3", "sync=false",
        ]  # fmt: skip
        rfd, wfd = os.pipe()
        try:
            self._proc = subprocess.Popen(
                ["/bin/sh", "-c", _SUPERVISOR, "lookfocus-camera", str(wfd), gst, "-v", *pipeline],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                pass_fds=(wfd,),
            )
        except OSError:
            os.close(rfd)
            raise
        finally:
            os.close(wfd)
        self._rfd = rfd
        threading.Thread(target=self._read_messages, name="gst-messages", daemon=True).start()
        threading.Thread(target=self._read_frames, name="gst-frames", daemon=True).start()

    # --- reader threads ---
    def _read_messages(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = raw.decode(errors="replace").strip()
            m = _CAPS.search(line)
            if m and not self._sized.is_set():
                self._size = (int(m.group(1)), int(m.group(2)))
                self._sized.set()
            elif line.startswith(("ERROR", "WARNING")) or "error" in line.lower()[:40]:
                log.info("gst: %s", line[:300])
        with self._cond:
            self._dead = True
            self._cond.notify_all()
        self._sized.set()

    def _read_frames(self) -> None:
        if not self._sized.wait(15) or self._size == (0, 0):
            return
        w, h = self._size
        n = w * h * 3
        buf = bytearray(n)
        view = memoryview(buf)
        while True:
            got = 0
            while got < n:
                try:
                    k = os.readv(self._rfd, [view[got:]])
                except OSError:
                    k = 0
                if k <= 0:
                    with self._cond:
                        self._dead = True
                        self._cond.notify_all()
                    return
                got += k
            frame = np.frombuffer(bytes(buf), np.uint8).reshape(h, w, 3)
            with self._cond:
                self._frame, self._seq = frame, self._seq + 1
                self._cond.notify_all()

    # --- the cv2.VideoCapture subset capture.py uses ---
    def isOpened(self) -> bool:
        return self._proc is not None and not self._dead

    def wait_ready(self, timeout: float) -> bool:
        return self._sized.wait(timeout) and self._size != (0, 0)

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._size[0])
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._size[1])
        return 0.0

    def set(self, _prop: int, _value) -> bool:
        return False

    def grab(self, timeout: float = 1.0) -> bool:
        """Wait for a frame newer than the last one retrieved (so every read is a fresh one)."""
        with self._cond:
            ok = self._cond.wait_for(lambda: self._seq != self._taken or self._dead, timeout)
            return ok and self._seq != self._taken

    def retrieve(self) -> tuple[bool, np.ndarray | None]:
        with self._cond:
            if self._frame is None:
                return False, None
            self._taken = self._seq
            return True, self._frame

    def read(self) -> tuple[bool, np.ndarray | None]:
        return self.retrieve() if self.grab() else (False, None)

    def release(self) -> None:
        p, self._proc = self._proc, None
        if p is not None:
            try:
                if p.stdin is not None:
                    p.stdin.close()  # the supervisor kills the pipeline
                p.wait(2)
            except (OSError, subprocess.TimeoutExpired):
                p.kill()
        if self._rfd >= 0:
            os.close(self._rfd)
            self._rfd = -1
        with self._cond:
            self._dead = True
            self._cond.notify_all()


def open_pipewire(index: int, width: int, fps: float, timeout_s: float) -> tuple[GstCapture | None, str]:
    cams = _pw_cache or pipewire_cameras()
    if not 0 <= index < len(cams):
        cams = pipewire_cameras()
        if not 0 <= index < len(cams):
            return None, "no such PipeWire camera"
    cam = cams[index]
    try:
        cap = GstCapture(cam.node_name, width, fps)
    except (OSError, RuntimeError) as e:
        return None, str(e)
    t0 = time.monotonic()
    if not cap.wait_ready(timeout_s):
        cap.release()
        return None, f"no video from PipeWire within {timeout_s:.0f}s"
    log.info("PipeWire camera %r: %dx%d (%.2fs)", cam.description, *cap._size, time.monotonic() - t0)
    return cap, ""


# --------------------------------------------------------------------------------------------------
# other apps on the camera
# --------------------------------------------------------------------------------------------------


class SensorActivityMonitor(threading.Thread):
    """No-op stand-in for the Windows MF sensor activity monitor: with PipeWire, other apps share the camera
    instead of being blocked, so there is nothing to yield to (D-050)."""

    def __init__(self, callback) -> None:
        super().__init__(name="camera-activity", daemon=True)
        self.callback = callback
        self.started_ok = threading.Event()
        self.error = ""

    def run(self) -> None:
        self.started_ok.set()

    def stop(self, timeout: float = 3.0) -> None:
        pass


def process_name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""
