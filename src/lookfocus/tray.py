"""LookFocus tray app: the engine (daemon.Daemon) plus tray icon, notifications, pause hotkey, autostart,
session lock / sleep handling, config file and rotating logs.

Threads: pystray's icon loop (main thread; menu callbacks run here, so anything slow is handed to a worker),
SystemEvents (hotkey, lock, sleep, display), the engine's threads, a config-file watcher, and on demand a
calibration thread (owns the overlay window) and a debug-view thread (owns the HighGUI window).
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from lookfocus import autostart, paths
from lookfocus import calibration as C
from lookfocus import config as K
from lookfocus.displays import LayoutError, build_displays, require_multiple

log = logging.getLogger("lookfocus.tray")

STATE_COLORS = {
    "active": (46, 160, 67),
    "paused": (120, 120, 128),
    "no_face": (225, 150, 20),
    "calibrating": (40, 120, 220),
    "error": (205, 45, 45),
}
REASON_TEXT = {
    "user": "paused",
    "lock": "paused while locked",
    "sleep": "paused while asleep",
    "display": "paused while the display is off",
}
MB_OK, MB_YESNO, MB_ICONINFO, MB_ICONWARNING = 0x0, 0x4, 0x40, 0x30
MB_SETFOREGROUND, MB_TOPMOST, IDYES = 0x10000, 0x40000, 6
# Engine restarts after a thread died: at most this many within the window, then stay in the error state
# (a persistent failure, e.g. the privacy check, shouldn't loop forever).
RESTART_BUDGET, RESTART_WINDOW_S = 3, 600.0


def make_icon(state: str, size: int = 64) -> Image.Image:
    """Coloured disc with a white glyph per state (drawn, no image assets). Designed on a 64 px grid and
    scaled, so the 256 px exe icon has the same proportions as the tray icon."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    k, c = size / 64, size / 2

    def box(x0, y0, x1, y1):
        return (c + x0 * k, c + y0 * k, c + x1 * k, c + y1 * k)

    def px(n: float) -> int:
        return max(1, round(n * k))

    d.ellipse((2 * k, 2 * k, s - 3 * k, s - 3 * k), fill=STATE_COLORS.get(state, STATE_COLORS["error"]))
    w = (255, 255, 255, 255)
    if state == "active":  # an eye: ring + pupil
        d.ellipse(box(-18, -12, 18, 12), outline=w, width=px(4))
        d.ellipse(box(-6, -6, 6, 6), fill=w)
    elif state == "paused":
        d.rectangle(box(-12, -14, -4, 14), fill=w)
        d.rectangle(box(4, -14, 12, 14), fill=w)
    elif state == "no_face":
        d.ellipse(box(-13, -13, 13, 13), outline=w, width=px(4))
    elif state == "calibrating":
        d.ellipse(box(-14, -14, 14, 14), outline=w, width=px(3))
        d.line(box(-20, 0, 20, 0), fill=w, width=px(3))
        d.line(box(0, -20, 0, 20), fill=w, width=px(3))
    else:  # error: "!"
        d.rectangle(box(-4, -18, 4, 6), fill=w)
        d.ellipse(box(-5, 10, 5, 20), fill=w)
    return img


def setup_logging(level: str) -> Path:
    """Rotating log in %APPDATA%\\LookFocus\\logs (text only: no images, no keystrokes are ever logged)."""
    log_dir = paths.data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "lookfocus.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler):
            root.removeHandler(h)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)
    return path


def install_crash_logging() -> None:
    """pythonw has no stderr: send uncaught exceptions (any thread) to the log file instead of nowhere."""

    def hook(exc_type, exc, tb) -> None:
        logging.getLogger("lookfocus").critical("uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_hook(args) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        info = (args.exc_type, args.exc_value, args.exc_traceback)
        logging.getLogger("lookfocus").critical("uncaught exception in thread %s", name, exc_info=info)

    sys.excepthook = hook
    threading.excepthook = thread_hook


def message_box(text: str, flags: int = MB_OK | MB_ICONINFO) -> int:
    user32 = ctypes.WinDLL("user32")
    user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    return user32.MessageBoxW(None, text, "LookFocus", flags | MB_SETFOREGROUND | MB_TOPMOST)


# pystray's Windows backend isn't thread-safe: replacing the icon checks the old HICON and then destroys it,
# so two threads updating at once destroyed the same handle twice ("WinError 1402: Invalid cursor handle",
# seen in the user's log on resume, where the hotkey and the engine both update the icon) and the second
# caller's handler aborted. Every change to the tray icon, title, menu or balloons goes through this lock.
ICON_LOCK = threading.RLock()


class _TrayIcon(pystray.Icon):
    """pystray's Windows backend shows a menu built at the last update_menu(); rebuild it on every
    right-click so the status line and the camera list are current (relies on pystray 0.19.5 internals)."""

    def _on_notify(self, wparam, lparam):
        if lparam == 0x0205:  # WM_RBUTTONUP
            with ICON_LOCK, contextlib.suppress(Exception):
                self._update_menu()
        return super()._on_notify(wparam, lparam)


class TrayApp:
    def __init__(self) -> None:
        self.data = paths.data_dir()
        self.cfg_path = self.data / "config.json"
        self.cal_path = self.data / "calibration.json"
        self.state_path = self.data / "state.json"
        self.cfg, self.cfg_problems = K.load(self.cfg_path)
        if not self.cfg_path.exists():
            # First run: match a calibration made earlier with `lookfocus calibrate --zones N`.
            with contextlib.suppress(ValueError, OSError):
                cal = C.load(self.cal_path)
                if cal is not None and cal.zones != self.cfg.zones:
                    self.cfg = replace(self.cfg, zones=cal.zones)
            K.save(self.cfg, self.cfg_path)
        self.log_path = setup_logging(self.cfg.log_level)
        install_crash_logging()
        self._cfg_mtime = self._mtime()
        self.lock = threading.RLock()
        self.daemon = None
        self.reasons: set[str] = set()
        self.state, self.detail = "no_face", "starting"
        self.calibrating = False
        self._overlay = None
        self._viewer: threading.Thread | None = None
        self._viewer_stop = threading.Event()
        self._halt = threading.Event()
        self._icons = {s: make_icon(s) for s in STATE_COLORS}
        self._uipi_warned = False
        self._cameras: tuple[float, list] = (0.0, [])
        self._monitors: tuple[float, int] = (0.0, 1)
        self._restarts: deque[float] = deque()
        from lookfocus.sysevents import SystemEvents

        self.sys = SystemEvents(self._on_system, self.cfg.hotkey)
        self.icon = _TrayIcon("LookFocus", self._icons["no_face"], "LookFocus: starting", menu=self._menu())

    # --- lifecycle -----------------------------------------------------------------------------
    def run(self) -> None:
        log.info("LookFocus tray starting (config %s)", self.cfg_path)
        self.icon.run(setup=self._setup)

    def _setup(self, icon) -> None:
        icon.visible = True
        self.sys.start()
        for p in self.cfg_problems:
            log.warning("config: %s", p)
        if self.cfg_problems:
            self.notify("Config problems", "; ".join(self.cfg_problems)[:250])
        try:
            if autostart.refresh():
                log.info("Start with Windows now points at this copy: %s", autostart.command())
        except OSError as e:
            log.warning("couldn't update the autostart entry: %s", e)
        threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True).start()
        self._start_engine()

    def quit(self) -> None:
        self._halt.set()
        if self._overlay is not None:
            self._overlay.cancelled = True
        self._close_viewer()
        self._stop_engine()
        self.sys.stop()
        self.icon.stop()

    def _engine_settings(self):
        from lookfocus.daemon import settings_from_config

        return settings_from_config(self.cfg)

    def _start_engine(self) -> None:
        try:
            self._start_engine_unchecked()
        except Exception as e:  # e.g. a Win32 call failing at sign-in: show it instead of dying silently
            log.exception("engine failed to start")
            self._set_state("error", f"couldn't start: {type(e).__name__}")
            detail = f"{type(e).__name__}: {e}"[:180]
            self.notify("LookFocus couldn't start", f"{detail} (see {self.log_path})")

    def _start_engine_unchecked(self) -> None:
        from lookfocus import monitors
        from lookfocus.daemon import Daemon

        with self.lock:
            if self.daemon is not None or self.calibrating or self._halt.is_set():
                return
            try:
                cal = C.load(self.cal_path)
            except ValueError as e:
                cal = None
                log.warning("calibration unreadable: %s", e)
            ds = build_displays(monitors.list_monitors(), self.cfg.zones)
            try:
                require_multiple(ds, self.cfg.zones)
                one_display = False
            except LayoutError:
                one_display = True
            if cal is None:
                self._set_state("error", "not set up yet: choose Calibrate")
                self._welcome(one_display)
                return
            if one_display:
                self._set_state("error", "only one display")
                self.notify(
                    "Only one display",
                    "Connect your other monitor, or split this one: LookFocus menu > Screen zones.",
                )
                return
            missing, added = cal.check_layout(ds)
            if missing or added:
                self._set_state("error", "display layout changed: recalibrate")
                self.notify("Display layout changed", "Choose Recalibrate in the LookFocus tray menu.")
                return
            self.daemon = Daemon(
                self._engine_settings(), cal, on_status=self._on_status, on_event=self._on_event
            )
            self.daemon.start()
            if self.reasons:
                self.daemon.set_paused(True)
                self._show_paused()
            log.info("engine started (%d displays)", len(ds))

    def _stop_engine(self) -> None:
        with self.lock:
            d, self.daemon = self.daemon, None
        if d is not None:
            self._close_viewer()
            d.stop()
            log.info("engine stopped")

    def _restart_engine(self, delay: float = 0.0) -> None:
        def work():
            if delay:
                time.sleep(delay)
            self._stop_engine()
            self._start_engine()

        threading.Thread(target=work, name="engine-restart", daemon=True).start()

    # --- state, icon, notifications ------------------------------------------------------------
    def _set_state(self, state: str, detail: str = "") -> None:
        with ICON_LOCK:
            changed = state != self.state
            self.state, self.detail = state, detail
            title = f"LookFocus: {detail or state}"[:127]
            try:
                if changed:  # same picture for "facing Zone 1" and "facing Zone 2": don't reload it
                    self.icon.icon = self._icons.get(state, self._icons["error"])
                if title != self.icon.title:
                    self.icon.title = title
            except Exception:  # a tray glitch must never abort the caller (hotkey, engine status)
                log.exception("tray icon update failed")
            self._refresh_menu()

    def _refresh_menu(self) -> None:
        with ICON_LOCK, contextlib.suppress(Exception):
            self.icon.update_menu()

    def _show_paused(self) -> None:
        order = ["user", "lock", "sleep", "display"]
        why = next((r for r in order if r in self.reasons), "user")
        self._set_state("paused", REASON_TEXT[why])

    def notify(self, title: str, body: str) -> None:
        log.info("notify: %s: %s", title, body)
        if self.cfg.notifications:
            with ICON_LOCK, contextlib.suppress(Exception):
                self.icon.notify(body[:255], title[:63])

    def _status_text(self) -> str:
        return f"{self.detail[:60] or self.state}"

    # --- engine callbacks (engine threads) -----------------------------------------------------
    def _on_status(self, status: str, detail: str) -> None:
        if self.calibrating:
            return
        if status == "active":
            self._set_state("active", f"facing {detail}" if detail else "watching")
        elif status == "paused":
            self._show_paused()
        elif status == "no_face":
            self._set_state("no_face", detail or "no face in view")
        elif status == "camera":
            busy = any(w in detail for w in ("released", "in use"))
            self._set_state("paused" if busy else "no_face", detail or "camera")
        elif status == "layout_changed":
            self._set_state("error", "display layout changed: recalibrate")
        else:
            self._set_state("no_face", detail or status)

    def _on_event(self, kind: str, info: dict) -> None:
        if kind in ("camera_yielded", "camera_busy"):
            self.notify(info.get("title", "Camera released"), info.get("body", ""))
        elif kind == "layout_changed":
            self.notify(
                "Display layout changed",
                "LookFocus paused switching. Choose Recalibrate in the LookFocus tray menu.",
            )
        elif kind == "camera_mode_changed":
            now, cal = info.get("now"), info.get("calibrated")
            self.notify(
                "Camera resolution changed",
                f"The camera now sends {now[0]}x{now[1]} (calibrated at {cal[0]}x{cal[1]}), for example "
                "after changing the camera-sharing setting. If switching feels off, recalibrate.",
            )
        elif kind == "focus_failed" and "UIPI" in info.get("reason", "") and not self._uipi_warned:
            self._uipi_warned = True
            self.notify(
                "Can't switch away from an administrator window",
                "Windows blocks this unless LookFocus also runs as administrator "
                "(lookfocus tray --elevated).",
            )
        elif kind == "camera_sharing":
            self._maybe_prompt_sharing(info)

    # --- system events (SystemEvents thread) ---------------------------------------------------
    def _on_system(self, event: str) -> None:
        log.info("system event: %s", event)
        if event == "hotkey":
            self.toggle_pause(announce=True)
            return
        if event.startswith("hotkey_failed:"):
            self.notify(
                "Pause hotkey unavailable", f'{event.split(":", 1)[1]}. Change "hotkey" in the config.'
            )
            return
        changes = {
            "lock": ("lock", True),
            "unlock": ("lock", False),
            "suspend": ("sleep", True),
            "resume": ("sleep", False),
            "display_off": ("display", True),
            "display_on": ("display", False),
        }
        if event in changes:
            reason, on = changes[event]
            self._set_reason(reason, on)
        elif event == "display_change" and self.daemon is None and not self.calibrating:
            self._restart_engine(delay=2.0)  # e.g. the monitor that was missing is back

    def _set_reason(self, reason: str, on: bool) -> None:
        with self.lock:
            (self.reasons.add if on else self.reasons.discard)(reason)
            paused = bool(self.reasons)
            if self.daemon is not None:
                self.daemon.set_paused(paused)
        if paused:
            self._show_paused()
        elif self.daemon is not None:
            self._set_state("no_face", "resuming")

    def toggle_pause(self, announce: bool = False) -> None:
        on = "user" not in self.reasons
        self._set_reason("user", on)
        if announce:
            self.notify(
                "LookFocus paused" if on else "LookFocus resumed",
                f"Press {self.cfg.hotkey} again to {'resume' if on else 'pause'}.",
            )

    # --- menu ----------------------------------------------------------------------------------
    def _menu(self) -> pystray.Menu:
        item, sep = pystray.MenuItem, pystray.Menu.SEPARATOR
        return pystray.Menu(
            item(lambda _: self._status_text(), None, enabled=False),
            sep,
            item(
                lambda _: "Resume" if "user" in self.reasons else "Pause",
                lambda: self.toggle_pause(),
                default=True,
            ),
            item(
                lambda _: "Calibrate..." if not self.cal_path.exists() else "Recalibrate...",
                lambda: self.start_calibration(),
                enabled=lambda _: not self.calibrating,
            ),
            item("Cancel calibration", lambda: self.cancel_calibration(), visible=lambda _: self.calibrating),
            item("Screen zones", pystray.Menu(self._zone_items)),
            item("Choose camera", pystray.Menu(self._camera_items)),
            item("Camera sharing...", lambda: self._run(self.camera_sharing_dialog)),
            item("Open config", lambda: self.open_config()),
            item(
                lambda _: "Close debug view" if self._viewer else "Debug view",
                lambda: self.toggle_viewer(),
                enabled=lambda _: self.daemon is not None or self._viewer is not None,
            ),
            item(
                "Start with Windows",
                lambda: self.toggle_autostart(),
                checked=lambda _: autostart.is_enabled(),
            ),
            sep,
            item("Quit", lambda: self.quit()),
        )

    def _run(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _camera_list(self) -> list:
        from lookfocus.camera_enum import list_cameras

        t, cams = self._cameras
        if time.monotonic() - t > 5:
            try:
                cams = list_cameras(self.cfg.camera_backend)
            except Exception:
                log.exception("camera enumeration failed")
                cams = []
            self._cameras = (time.monotonic(), cams)
        return cams

    def _camera_items(self):
        from lookfocus.camera_enum import device_key

        cfg = self.cfg
        auto = not (cfg.camera_path or cfg.camera_name or cfg.camera_index is not None)
        yield pystray.MenuItem(
            "Automatic", lambda: self.choose_camera(None), checked=lambda _: auto, radio=True
        )
        for cam in self._camera_list():
            chosen = (
                bool(cfg.camera_path)
                and bool(cam.path)
                and device_key(cam.path) == device_key(cfg.camera_path)
            )
            label = cam.name + ("  (IR?)" if cam.looks_ir else "")
            yield pystray.MenuItem(
                label,
                functools.partial(self._camera_clicked, cam),  # pystray rejects lambdas with >2 params
                checked=lambda _, ch=chosen: ch,
                radio=True,
            )

    def _monitor_count(self) -> int:
        from lookfocus import monitors

        t, n = self._monitors
        if time.monotonic() - t > 5:
            try:
                n = len(monitors.list_monitors())
            except Exception:
                log.exception("monitor enumeration failed")
            self._monitors = (time.monotonic(), n)
        return n

    def _zone_items(self):
        several = self._monitor_count() >= 2
        yield pystray.MenuItem(
            "Off: one per monitor",
            functools.partial(self._zones_clicked, 0),
            checked=lambda _: self.cfg.zones == 0,
            radio=True,
            enabled=several,  # with one monitor, "off" leaves nothing to switch between
        )
        for n in (2, 3, 4):
            yield pystray.MenuItem(
                f"Split {'the main' if several else 'this'} monitor in {n}",
                functools.partial(self._zones_clicked, n),
                checked=lambda _, n=n: self.cfg.zones == n,
                radio=True,
            )

    # --- actions -------------------------------------------------------------------------------
    def _zones_clicked(self, n, _icon=None, _item=None) -> None:
        self.set_zones(n)

    def set_zones(self, n: int) -> None:
        """Change the zone count and recalibrate right away: the old calibration can't fit new zones.
        The engine isn't restarted separately; calibration stops it and starts it again afterwards."""
        if n == self.cfg.zones or self.calibrating:
            return
        self.cfg = replace(self.cfg, zones=n)
        K.save(self.cfg, self.cfg_path)
        self._cfg_mtime = self._mtime()
        log.info("screen zones set to %d from the menu", n)
        self.start_calibration()

    def _welcome(self, one_display: bool) -> None:
        """First run (no calibration yet): offer to set up, once. Later starts just say what's missing."""
        st = self._load_state()
        if st.get("welcomed"):
            how = "choose Screen zones" if one_display else "choose Calibrate"
            self.notify("LookFocus isn't set up yet", f"Right-click the LookFocus tray icon and {how}.")
            return
        st["welcomed"] = True
        self._save_state(st)
        self._run(lambda: self._welcome_dialog(one_display))

    def _welcome_dialog(self, one_display: bool) -> None:
        if one_display:
            text = (
                "Welcome to LookFocus.\n\nYou have one monitor. LookFocus can split it into a left and a "
                "right half that work like two screens: look at a half and type, and the window you last "
                "used there gets the keyboard.\n\nFirst it needs to learn how you look at each half: a dot "
                "appears, you look at it the way you normally would. It takes about a minute.\n\n"
                "Split the screen and start now?"
            )
        else:
            text = (
                "Welcome to LookFocus.\n\nFirst it needs to learn how you look at each of your screens: "
                "a dot appears on each one, you look at it the way you normally would. It takes about a "
                "minute.\n\nStart now?"
            )
        if message_box(text, MB_YESNO | MB_ICONINFO) != IDYES:
            how = "Screen zones" if one_display else "Calibrate"
            self.notify("Set up later", f"Right-click the LookFocus tray icon and choose {how} when ready.")
            return
        if one_display:
            self.set_zones(2)
        else:
            self.start_calibration()

    def _camera_clicked(self, cam, _icon=None, _item=None) -> None:
        self.choose_camera(cam)

    def choose_camera(self, cam) -> None:
        if cam is None:
            new = replace(self.cfg, camera_path="", camera_name="", camera_index=None)
        else:
            new = replace(self.cfg, camera_path=cam.path, camera_name=cam.name, camera_index=None)
        self._apply_config(new, save=True)

    def open_config(self) -> None:
        if not self.cfg_path.exists():
            K.save(self.cfg, self.cfg_path)
        try:
            os.startfile(self.cfg_path)
        except OSError:
            subprocess.Popen(["notepad.exe", str(self.cfg_path)])

    def toggle_autostart(self) -> None:
        try:
            autostart.set_enabled(not autostart.is_enabled())
        except OSError as e:
            self.notify("Start with Windows", f"Couldn't change the setting: {e}")
        self._refresh_menu()

    # --- config file ---------------------------------------------------------------------------
    def _mtime(self) -> float:
        try:
            return self.cfg_path.stat().st_mtime
        except OSError:
            return 0.0

    def _housekeeping(self) -> None:
        """Every 2 s: apply config-file edits, and restart the engine if one of its threads died."""
        while not self._halt.wait(2.0):
            for step in (self._check_config, self._check_engine):
                try:
                    step()
                except Exception:
                    log.exception("%s failed", step.__name__)

    def _check_config(self) -> None:
        m = self._mtime()
        if m and m != self._cfg_mtime:
            self._cfg_mtime = m
            cfg, problems = K.load(self.cfg_path)
            for p in problems:
                log.warning("config: %s", p)
            if problems:
                self.notify("Config problems", "; ".join(problems)[:250])
            self._apply_config(cfg, save=False)

    def _check_engine(self) -> None:
        d = self.daemon
        dead = d.dead_threads() if d is not None else []
        if not dead:
            return
        now = time.monotonic()
        while self._restarts and now - self._restarts[0] > RESTART_WINDOW_S:
            self._restarts.popleft()
        _seq, last = d.poses.get()
        why = (last.message if last is not None and last.message else "") or ", ".join(dead)
        log.error("engine thread(s) stopped: %s (last camera status: %s)", dead, why)
        if len(self._restarts) >= RESTART_BUDGET:
            self._stop_engine()
            self._set_state("error", f"stopped: {why}")
            self.notify("LookFocus stopped", f"{why}"[:180] + f". Details in {self.log_path}")
            return
        self._restarts.append(now)
        self._stop_engine()
        self._start_engine()

    def _apply_config(self, new: K.Config, save: bool) -> None:
        old, self.cfg = self.cfg, new
        if save:
            K.save(new, self.cfg_path)
            self._cfg_mtime = self._mtime()
        if new.hotkey != old.hotkey:
            self.sys.set_hotkey(new.hotkey)
        if new.log_level != old.log_level:
            logging.getLogger().setLevel(new.log_level)
        app_only = {"hotkey", "log_level", "notifications"}
        if any(getattr(new, f) != getattr(old, f) for f in K.to_dict(new) if f not in app_only):
            log.info("config changed: restarting the engine")
            self._restart_engine()

    # --- calibration ---------------------------------------------------------------------------
    def start_calibration(self) -> None:
        with self.lock:
            if self.calibrating:
                return
            self.calibrating = True
        self._set_state("calibrating", "calibrating: look at the dots")
        threading.Thread(target=self._calibrate, name="calibration", daemon=True).start()

    def cancel_calibration(self) -> None:
        if self._overlay is not None:
            self._overlay.cancelled = True

    def _calibrate(self) -> None:
        from lookfocus import monitors
        from lookfocus import overlay as O
        from lookfocus import windows_focus as wf
        from lookfocus.calibrator import Calibrator, CalibratorSettings, Cancelled, separability
        from lookfocus.capture import CaptureThread, LatestSlot
        from lookfocus.daemon import capture_settings

        self._stop_engine()  # release the camera: calibration runs its own capture
        capture = ov = None
        try:
            ds = build_displays(monitors.list_monitors(), self.cfg.zones)
            require_multiple(ds, self.cfg.zones)
            slot = LatestSlot()
            capture = CaptureThread(capture_settings(self.cfg, probe_sharing=False), slot)
            capture.start()
            ov = self._overlay = O.Overlay()
            cal = Calibrator(
                slot,
                ov,
                CalibratorSettings(source=self.cfg.pose_source),
                cancel_hint="Press Esc to cancel (or use the LookFocus tray menu).",
                focus=lambda h: wf.focus_window(h, allow_alt=False),
            ).run(ds, self.cfg.zones)
            C.save(cal, self.cal_path)
            problems, _report = separability(cal, self.cfg.pose_source)
            if problems:
                self.notify(
                    "Calibration saved, with a warning", problems[0][:200] + ". Consider recalibrating."
                )
            else:
                tip = "" if autostart.is_enabled() else " Tip: tick Start with Windows in the menu."
                self.notify(
                    "Calibration saved", "All displays can be told apart. LookFocus is running." + tip
                )
        except Cancelled:
            self.notify("Calibration cancelled", "Nothing was changed.")
        except LayoutError:
            self.notify("Can't calibrate", "Only one display: split it with LookFocus menu > Screen zones.")
        except (TimeoutError, RuntimeError) as e:
            self.notify("Calibration failed", str(e)[:250])
        except Exception as e:
            log.exception("calibration crashed")
            self.notify("Calibration failed", f"{type(e).__name__}: {e}"[:250])
        finally:
            if ov is not None:
                ov.close()
            self._overlay = None
            if capture is not None:
                capture.stop()
            self.calibrating = False
            self._set_state("no_face", "starting")
            self._start_engine()

    # --- camera sharing (the user-requested optional setup step, as a dialog) -------------------
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, st: dict) -> None:
        with contextlib.suppress(OSError):
            self.state_path.write_text(json.dumps(st), encoding="utf-8")

    def _maybe_prompt_sharing(self, info: dict) -> None:
        """Once (first time the setting reads OFF on a build that has it), offer to open the setting."""
        if info.get("state") != "off" or not info.get("available"):
            return
        st = self._load_state()
        if st.get("sharing_prompted"):
            return
        st["sharing_prompted"] = True
        self._save_state(st)
        self._run(self.camera_sharing_dialog)

    def camera_sharing_dialog(self) -> None:
        from lookfocus import camera_sharing as S

        d = self.daemon
        if d is None:
            state, camera = S.check_now(self._engine_settings().capture)
            available = S.feature_available(S.windows_build())
        else:
            state, camera, available = (
                d.capture.sharing,
                d.capture.settings.camera.name,
                d.capture.sharing_available,
            )
            if state is S.Sharing.UNKNOWN:
                message_box(
                    "LookFocus is still checking the camera-sharing setting. Try again in a few seconds."
                )
                return
        name = f"'{S.SETTING_NAME}'"
        if state is S.Sharing.UNSUPPORTED or not available:
            message_box(
                f"This Windows version doesn't have the {name} setting (it needs Windows 11 24H2 or "
                "later).\n\nWhen another app, such as a video call, starts using the camera, LookFocus "
                "releases it and tells you, then resumes afterwards."
            )
        elif state is S.Sharing.ON:
            message_box(f"{name} is ON.\n\nVideo calls and LookFocus can use the camera at the same time.")
        elif state is S.Sharing.OFF:
            where = S.SETTINGS_PATH.format(camera=camera or "your camera")
            answer = message_box(
                f"{name} is OFF, so only one app can get camera images at a time. When another app (for "
                "example a video call) starts using the camera, LookFocus releases it and tells you, then "
                f"resumes when that app is done.\n\nTo let both work at once, turn it on in:\n{where}\n\n"
                "Open camera settings now?",
                MB_YESNO | MB_ICONINFO,
            )
            if answer == IDYES:
                S.open_camera_settings()
        else:
            message_box(
                "Couldn't check the camera-sharing setting (camera unavailable or in use?).",
                MB_OK | MB_ICONWARNING,
            )

    # --- debug view ----------------------------------------------------------------------------
    def toggle_viewer(self) -> None:
        if self._viewer is not None:
            self._close_viewer()
        elif self.daemon is not None:
            self._viewer_stop.clear()
            self._viewer = threading.Thread(
                target=self._view, args=(self.daemon,), name="debug-view", daemon=True
            )
            self._viewer.start()
        self._refresh_menu()

    def _close_viewer(self) -> None:
        v = self._viewer
        if v is not None:
            self._viewer_stop.set()
            if v is not threading.current_thread():
                v.join(3)
            self._viewer = None

    def _view(self, d) -> None:
        from lookfocus.capture import LatestSlot
        from lookfocus.debugview import run_viewer

        frames: LatestSlot = LatestSlot()
        d.capture.debug_hook = lambda f, p, s: frames.put((f, p, s))

        def lines() -> list[str]:
            cur = d.display_by_id.get(d.machine.current or "")
            return [
                f"LookFocus: {self.state} ({self.detail})",
                f"facing: {cur.short if cur else '-'}   decision: {d.machine.state}",
                f"camera sharing: {d.capture.sharing.value}",
            ]

        try:
            run_viewer(
                frames,
                d.poses,
                extra_lines=lines,
                stop=self._viewer_stop,
                on_key=lambda k: d.capture.resume_from_yield() if k == ord("y") else None,
                report=lambda text: log.info("guided check:\n%s", text),
            )
        except Exception:
            log.exception("debug view failed")
        finally:
            d.capture.debug_hook = None
            self._viewer = None
            self._refresh_menu()


def main(elevated: bool = False) -> int:
    from lookfocus import monitors, windows_focus
    from lookfocus.sysevents import claim_single_instance

    if elevated and not windows_focus.we_are_elevated():
        cmd = paths.app_command("tray")
        handle = windows_focus.start_elevated(cmd[0], cmd[1:], show=1)
        return 0 if handle else 1
    monitors.enable_dpi_awareness()
    if not claim_single_instance():
        message_box("LookFocus is already running (see the tray icon).")
        return 0
    TrayApp().run()
    return 0
