"""Windows 11 multi-app camera ("Allow multiple apps to use camera at the same time").

With the setting off, a camera streams to one app at a time: a second app's open "succeeds" but it gets
no frames (measured, DECISIONS.md D-011). With it on, both apps get frames.

Windows exposes neither an API nor a documented registry value for the setting (Microsoft Q&A, 2025).
So:
- **available?** Windows build check (the setting shipped with 24H2 build 26100.3321; the Settings page
  moved under "Advanced camera options" in 26100/26200.8524). A heuristic: it may still be missing on a
  build that has it staged, and the setup step says so.
- **on?** Behavioural probe, as Microsoft suggests: while LookFocus holds the camera, a child process opens
  the same camera and reports whether it receives real frames.
- **hint:** Frame Server's per-camera sensor-group key carries an undocumented `MEPSGFlag` (1 on the dev
  machine with the setting on). Logged next to the probe result, never trusted on its own.

Probe child: python -m lookfocus.camera_sharing --probe-child --backend msmf --index 0
"""

from __future__ import annotations

import argparse
import enum
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from lookfocus.camera_usage import SETTING_NAME

log = logging.getLogger(__name__)

MIN_BUILD = (26100, 3321)
SETTINGS_URI = "ms-settings:camera"  # Settings > Bluetooth & devices > Cameras
SENSOR_GROUPS_KEY = r"SOFTWARE\Microsoft\Windows Media Foundation\FrameServer\SensorGroups"

SETTINGS_PATH = "Settings > Bluetooth & devices > Cameras > {camera} > Advanced camera options"


class Sharing(enum.Enum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"  # probe not run yet or inconclusive
    UNSUPPORTED = "unsupported"  # this Windows build predates the setting


@dataclass(frozen=True)
class WindowsBuild:
    build: int
    ubr: int
    display: str  # e.g. "25H2"

    def __str__(self) -> str:
        return f"{self.build}.{self.ubr} ({self.display})" if self.display else f"{self.build}.{self.ubr}"


def feature_available(b: WindowsBuild) -> bool:
    return (b.build, b.ubr) >= MIN_BUILD


def windows_build() -> WindowsBuild:
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:

        def get(name, default):
            try:
                return winreg.QueryValueEx(k, name)[0]
            except OSError:
                return default

        return WindowsBuild(int(get("CurrentBuild", 0)), int(get("UBR", 0)), str(get("DisplayVersion", "")))


def frame_server_hint(camera_path: str) -> int | None:
    """Undocumented MEPSGFlag of the sensor group publishing this camera, if any (diagnostics only)."""
    import winreg

    from lookfocus.camera_enum import device_key

    want = device_key(camera_path)
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SENSOR_GROUPS_KEY)
    except OSError:
        return None
    with root:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                return None
            i += 1
            try:
                with winreg.OpenKey(root, name) as g:
                    link = str(winreg.QueryValueEx(g, "SymbolicLinkName")[0])
                    if device_key(link) != want:
                        continue
                    try:
                        return int(winreg.QueryValueEx(g, "MEPSGFlag")[0])
                    except OSError:
                        return 0
            except OSError:
                continue


def probe_command(backend: str, index: int, seconds: float) -> list[str]:
    args = ["--probe-child", "--backend", backend, "--index", str(index), "--seconds", str(seconds)]
    if getattr(sys, "frozen", False):  # PyInstaller build: the app exe dispatches --probe-child itself
        return [sys.executable, *args]
    return [sys.executable, "-m", "lookfocus.camera_sharing", *args]


def probe_while_holding(
    backend: str,
    index: int,
    seconds: float = 2.0,
    timeout_s: float = 20.0,
    on_child_pid: Callable[[int], None] | None = None,
) -> Sharing:
    """Call ONLY while this process holds camera `index` open and streaming: can a second app see it?

    The child prints its own PID before it opens the camera, so the caller can recognise it in camera
    activity reports (Popen's PID would be the venv launcher's, not the interpreter that opens the camera).
    """
    try:
        proc = subprocess.Popen(
            probe_command(backend, index, seconds),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        log.warning("camera sharing probe failed to run: %s", e)
        return Sharing.UNKNOWN
    watchdog = threading.Timer(timeout_s, proc.kill)
    watchdog.start()
    info = None
    try:
        for line in proc.stdout:
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "pid" in msg and on_child_pid is not None:
                on_child_pid(int(msg["pid"]))
            if "opened" in msg:
                info = msg
        proc.wait()
    finally:
        watchdog.cancel()
    if info is None:
        log.warning("camera sharing probe gave no result (rc=%s)", proc.returncode)
        return Sharing.UNKNOWN
    log.info("camera sharing probe: %s", info)
    if not info.get("opened"):
        return Sharing.UNKNOWN
    return Sharing.ON if info.get("real") else Sharing.OFF


def _child(backend: str, index: int, seconds: float) -> dict:
    """Probe child: open the camera like a second app would, report whether real frames arrive."""
    print(json.dumps({"pid": os.getpid()}), flush=True)  # lets the parent recognise us in activity reports
    from lookfocus.capture import motion, open_camera, thumbnail

    res = open_camera(index, backend, timeout_s=10)
    out: dict = {"opened": res.ok, "open_s": round(res.open_s, 2), "error": res.error}
    if not res.ok:
        return out
    frames, lumas, motions, prev = 0, [], [], None
    t_end = time.monotonic() + seconds
    try:
        while time.monotonic() < t_end:
            ok, frame = res.cap.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            frames += 1
            th = thumbnail(frame)
            lumas.append(float(th.mean()))
            if prev is not None:
                motions.append(motion(prev, th))
            prev = th
    finally:
        res.cap.release()
    # Blocked second clients get nothing, or a few all-black frames (DSHOW); a live sensor shows light
    # or at least sensor noise.
    lit = sum(v > 0.5 for v in lumas)
    noisy = sum(m > 0.05 for m in motions)
    out.update(frames=frames, lit=lit, noisy=noisy, real=frames >= 5 and (lit >= frames // 2 or noisy >= 3))
    return out


def open_camera_settings() -> None:
    """Open Settings > Bluetooth & devices > Cameras (user action only)."""
    os.startfile(SETTINGS_URI)


def check_now(settings=None) -> tuple[Sharing, str]:
    """Open the camera, probe, release. Returns (state, camera name). For the setup step."""
    from lookfocus.capture import CameraError, CameraSource, CaptureSettings

    src = CameraSource(settings or CaptureSettings())
    try:
        src.open()
    except CameraError as e:
        log.warning("camera sharing check: camera unavailable: %s", e)
        return Sharing.UNKNOWN, ""
    try:
        time.sleep(0.5)  # let the stream settle before a second client connects
        return probe_while_holding(src.backend, src.index), src.name
    finally:
        src.release()


def run_setup_step(settings=None, *, ask=input, say=print) -> Sharing:
    """Optional setup step: explain the Windows camera-sharing setting and offer to turn it on."""
    say("Camera sharing (optional)")
    try:
        build = windows_build()
    except OSError:
        build = WindowsBuild(0, 0, "")
    if not feature_available(build):
        say(f"  Windows build {build} doesn't have the '{SETTING_NAME}' setting")
        say("  (it needs Windows 11 24H2 build 26100.3321 or later). When another app, such as a video call,")
        say("  starts using the camera, LookFocus releases it and resumes when that app is done.")
        return Sharing.UNSUPPORTED
    say(f"  Windows build {build} has the '{SETTING_NAME}' setting.")
    while True:
        say("  Checking whether it is on (opens the camera for a few seconds)...")
        state, camera = check_now(settings)
        if state is Sharing.ON:
            say("  It is ON: video calls and LookFocus can use the camera at the same time.")
            return state
        if state is Sharing.UNKNOWN:
            say("  Couldn't tell (camera unavailable or in use?). Re-run this check later with")
            say("  `lookfocus camera-sharing`.")
            return state
        say("  It is OFF. With it off, only one app can get camera images at a time, so whenever another")
        say("  app (e.g. a video call) starts using the camera, LookFocus releases it and tells you, then")
        say("  resumes when that app is done.")
        say("  To let both work at once, turn it on in:")
        say("    " + SETTINGS_PATH.format(camera=camera or "your camera"))
        answer = ask("  Open camera settings now? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            say("  Skipped; LookFocus will release the camera for other apps when needed.")
            return state
        open_camera_settings()
        answer = ask("  Turn the setting on, then press Enter to check again (or type s to skip): ")
        if answer.strip().lower() == "s":
            return state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe-child", action="store_true")
    ap.add_argument("--backend", default="msmf")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--seconds", type=float, default=2.0)
    args = ap.parse_args(argv)
    if args.probe_child:
        print(json.dumps(_child(args.backend, args.index, args.seconds)), flush=True)
        return 0
    run_setup_step()
    return 0


if __name__ == "__main__":
    sys.exit(main())
