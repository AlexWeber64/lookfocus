"""System events for the tray app on KDE Plasma (the counterpart of sysevents.py):

- "hotkey": the pause shortcut, a KDE global shortcut registered by the KWin bridge script;
- "lock" / "unlock": org.freedesktop.ScreenSaver ActiveChanged on the session bus (KDE's screen locker);
- "suspend" / "resume": logind's PrepareForSleep on the system bus;
- "display_off" / "display_on": every screen off / one back on (Wayland org_kde_kwin_dpms);
- "display_change": KWin's screensChanged (monitors added, removed or rearranged).

Also the single-instance lock (an flock on a file in $XDG_RUNTIME_DIR).
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from collections.abc import Callable
from queue import Empty, Queue

from jeepney import MatchRule
from jeepney.bus_messages import message_bus
from jeepney.io.threading import DBusRouter, open_dbus_connection
from jeepney.low_level import HeaderFields

from lookfocus.linux.kwin import get_bridge, runtime_dir
from lookfocus.linux.wayland import ActivityWatcher

log = logging.getLogger(__name__)

EventCallback = Callable[[str], None]

LOCK_RULE = MatchRule(type="signal", interface="org.freedesktop.ScreenSaver", member="ActiveChanged")
SLEEP_RULE = MatchRule(
    type="signal",
    interface="org.freedesktop.login1.Manager",
    member="PrepareForSleep",
    path="/org/freedesktop/login1",
)


class SystemEvents(threading.Thread):
    def __init__(self, callback: EventCallback, hotkey: str) -> None:
        super().__init__(name="system-events", daemon=True)
        self.callback = callback
        self.hotkey = hotkey  # registered by the bridge script (kwin.configure), not here
        self._halt = threading.Event()
        self._queue: Queue = Queue()
        self._routers: list[DBusRouter] = []
        self._locked: bool | None = None
        self._watcher: ActivityWatcher | None = None

    def stop(self) -> None:
        self._halt.set()
        self.join(2)
        if self._watcher is not None:
            self._watcher.stop()
        for r in self._routers:
            r.close()
            r.conn.close()

    def set_hotkey(self, hotkey: str) -> None:
        """KDE keeps global shortcuts in its own settings once registered; changing the config can't move
        it, so the tray points the user to System Settings instead (D-050)."""
        if hotkey != self.hotkey:
            self.hotkey = hotkey
            self._emit("hotkey_kde")

    def _emit(self, event: str) -> None:
        try:
            self.callback(event)
        except Exception:
            log.exception("system event handler failed")

    def _subscribe(self, bus: str, rule: MatchRule) -> None:
        try:
            router = DBusRouter(open_dbus_connection(bus))
            router.filter(rule, queue=self._queue)
            router.send_and_get_reply(message_bus.AddMatch(rule), timeout=3)
            self._routers.append(router)
        except Exception as e:
            log.warning("can't watch %s bus for %s: %s", bus.lower(), rule.header_fields.get("member"), e)

    def _on_bridge(self, kind: str, _data: dict) -> None:
        if kind == "hotkey":
            self._emit("hotkey")
        elif kind == "screens_changed":
            self._emit("display_change")

    def _on_screens(self, on: bool) -> None:
        self._emit("display_on" if on else "display_off")

    def run(self) -> None:
        try:
            get_bridge().add_listener(self._on_bridge)
        except Exception as e:
            log.warning("no KWin bridge for the hotkey and display changes: %s", e)
        self._watcher = ActivityWatcher(timeout_ms=60_000, on_screens=self._on_screens)
        self._watcher.start()
        self._subscribe("SESSION", LOCK_RULE)
        self._subscribe("SYSTEM", SLEEP_RULE)
        while not self._halt.is_set():
            try:
                msg = self._queue.get(timeout=0.5)
            except Empty:
                continue
            member = msg.header.fields.get(HeaderFields.member)
            if member == "ActiveChanged":
                locked = bool(msg.body[0])
                if locked != self._locked:  # KDE emits it on two object paths
                    self._locked = locked
                    self._emit("lock" if locked else "unlock")
            elif member == "PrepareForSleep":
                self._emit("suspend" if msg.body[0] else "resume")


_instance_fd: int | None = None


def claim_single_instance(name: str = "tray") -> bool:
    """True if no other LookFocus tray runs in this session (the lock lives as long as the process)."""
    global _instance_fd
    fd = os.open(runtime_dir() / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    _instance_fd = fd
    return True
