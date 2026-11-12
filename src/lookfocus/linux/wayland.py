"""A minimal Wayland client (pure Python, no libwayland): input-idle notifications and screen power.

Wayland gives ordinary clients no "time since the last input" query (KDE's GetSessionIdleTime answers
NotSupported on Wayland, measured), so LookFocus uses two protocols KWin offers every client:

- ext_idle_notifier_v1 (v2: get_input_idle_notification ignores idle inhibitors such as video players):
  "idled" after `timeout_ms` without input of any kind, "resumed" on the next input. That says *that* the
  user did something and when, never what: no key codes, and no keyboard object is ever bound.
- org_kde_kwin_dpms: each output's power mode (on / standby / suspend / off), to pause while all screens
  are off, as the Windows build does with GUID_CONSOLE_DISPLAY_STATE.

The wire format is the documented Wayland protocol: every message is (object id u32, size << 16 | opcode
u32) followed by 32-bit aligned arguments. Only the few requests and events used here are encoded.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

DISPLAY_ID = 1
DPMS_ON = 0


class WaylandError(RuntimeError):
    pass


def _string(s: str) -> bytes:
    raw = s.encode() + b"\0"
    return struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


def _read_string(buf: bytes, off: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("<I", buf, off)
    s = buf[off + 4 : off + 4 + n - 1].decode(errors="replace")
    return s, off + 4 + n + (-n % 4)


class Connection:
    """One Wayland connection with a fixed set of objects; events go to handlers by object id."""

    def __init__(self) -> None:
        path = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        if not path.startswith("/"):
            runtime = os.environ.get("XDG_RUNTIME_DIR")
            if not runtime:
                raise WaylandError("XDG_RUNTIME_DIR is not set")
            path = os.path.join(runtime, path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        try:
            self.sock.connect(path)
        except OSError as e:
            self.sock.close()
            raise WaylandError(f"can't connect to the Wayland display at {path}: {e}") from e
        self._next_id = 2
        self._buf = b""
        self.handlers: dict[int, Callable[[int, bytes], None]] = {}
        self.handlers[DISPLAY_ID] = self._on_display

    def new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def send(self, obj: int, opcode: int, payload: bytes = b"") -> None:
        size = 8 + len(payload)
        self.sock.sendall(struct.pack("<II", obj, (size << 16) | opcode) + payload)

    def dispatch(self, timeout: float | None = None) -> bool:
        """Read and handle whatever arrives within `timeout`; False if the compositor hung up."""
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(65536)
        except TimeoutError:
            return True
        except OSError:
            return False
        if not data:
            return False
        self._buf += data
        while len(self._buf) >= 8:
            obj, word = struct.unpack_from("<II", self._buf, 0)
            size, opcode = word >> 16, word & 0xFFFF
            if size < 8 or len(self._buf) < size:
                break
            body, self._buf = self._buf[8:size], self._buf[size:]
            handler = self.handlers.get(obj)
            if handler is not None:
                handler(opcode, body)
        return True

    def roundtrip(self, timeout: float = 2.0) -> None:
        """wl_display.sync and wait for its done event: everything sent before has been handled."""
        cb = self.new_id()
        done = threading.Event()
        self.handlers[cb] = lambda _op, _body: done.set()
        self.send(DISPLAY_ID, 0, struct.pack("<I", cb))
        end = time.monotonic() + timeout
        while not done.is_set():
            left = end - time.monotonic()
            if left <= 0 or not self.dispatch(left):
                raise WaylandError("no answer from the compositor")
        self.handlers.pop(cb, None)

    def _on_display(self, opcode: int, body: bytes) -> None:
        if opcode == 0:  # error(object, code, message)
            obj, code = struct.unpack_from("<II", body, 0)
            msg, _ = _read_string(body, 8)
            raise WaylandError(f"protocol error on object {obj} (code {code}): {msg}")
        # opcode 1: delete_id; ids are never reused here, so nothing to do

    def close(self) -> None:
        self.sock.close()


class Registry:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self.id = conn.new_id()
        self.globals: dict[str, list[tuple[int, int]]] = {}  # interface -> [(name, version)]
        conn.handlers[self.id] = self._on_event
        conn.send(DISPLAY_ID, 1, struct.pack("<I", self.id))  # wl_display.get_registry
        conn.roundtrip()

    def _on_event(self, opcode: int, body: bytes) -> None:
        if opcode == 0:  # global(name, interface, version)
            (name,) = struct.unpack_from("<I", body, 0)
            iface, off = _read_string(body, 4)
            (version,) = struct.unpack_from("<I", body, off)
            self.globals.setdefault(iface, []).append((name, version))

    def bind(self, iface: str, version: int, index: int = 0) -> int:
        name, available = self.globals[iface][index]
        new = self.conn.new_id()
        payload = struct.pack("<I", name) + _string(iface) + struct.pack("<II", min(version, available), new)
        self.conn.send(self.id, 0, payload)
        return new


class ActivityWatcher(threading.Thread):
    """Tracks when the last input of any kind happened, and whether every screen is off.

    `last_input()` is exact at the two moments the protocol reports: a "resumed" event (input just now)
    and an "idled" event (the last input was `timeout_ms` ago). In between, while the user keeps
    providing input without a `timeout_ms` gap, it stays at the start of the burst; with a short timeout
    that only happens during very fast continuous input, and the caller treats it as ongoing input anyway.
    """

    def __init__(
        self,
        timeout_ms: int = 40,
        on_screens: Callable[[bool], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="wayland-activity", daemon=True)
        self.timeout_ms = timeout_ms
        self.on_screens = on_screens  # called with True / False when any / no screen is on
        self.clock = clock
        self._lock = threading.Lock()
        self._last_input = clock()  # unknown before the first event: assume "just now"
        self._idle = False
        self._dpms: dict[int, int] = {}
        self._screens_on = True
        self._halt = threading.Event()
        self.ready = threading.Event()
        self.error: str = ""

    def last_input(self) -> float:
        with self._lock:
            return self._last_input

    @property
    def idle(self) -> bool:
        with self._lock:
            return self._idle

    def stop(self) -> None:
        self._halt.set()
        self.join(2)

    def run(self) -> None:
        try:
            conn = Connection()
        except WaylandError as e:
            self.error = str(e)
            self.ready.set()
            return
        try:
            self._setup(conn)
            self.ready.set()
            while not self._halt.is_set():
                if not conn.dispatch(0.25):
                    log.warning("Wayland compositor closed the connection")
                    break
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log.warning("Wayland activity watcher stopped: %s", self.error)
        finally:
            self.ready.set()
            conn.close()

    def _setup(self, conn: Connection) -> None:
        reg = Registry(conn)
        if "ext_idle_notifier_v1" not in reg.globals or "wl_seat" not in reg.globals:
            raise WaylandError("the compositor offers no ext_idle_notifier_v1 (needs KDE Plasma 6)")
        seat = reg.bind("wl_seat", 1)
        conn.handlers[seat] = lambda _op, _body: None  # capabilities: not needed
        notifier_version = reg.globals["ext_idle_notifier_v1"][0][1]
        notifier = reg.bind("ext_idle_notifier_v1", 2)
        note = conn.new_id()
        conn.handlers[note] = self._on_idle
        # v2: get_input_idle_notification (opcode 2) ignores inhibitors; v1 only has get_idle_notification.
        opcode = 2 if notifier_version >= 2 else 1
        conn.send(notifier, opcode, struct.pack("<III", note, self.timeout_ms, seat))
        if self.on_screens is not None and "org_kde_kwin_dpms_manager" in reg.globals:
            mgr = reg.bind("org_kde_kwin_dpms_manager", 1)
            for i in range(len(reg.globals.get("wl_output", []))):
                out = reg.bind("wl_output", 1, i)
                conn.handlers[out] = lambda _op, _body: None  # geometry / mode: not needed
                dpms = conn.new_id()
                conn.handlers[dpms] = lambda op, body, d=dpms: self._on_dpms(d, op, body)
                conn.send(mgr, 0, struct.pack("<II", dpms, out))  # get(new_id, output)
        conn.roundtrip()

    def _on_idle(self, opcode: int, _body: bytes) -> None:
        now = self.clock()
        with self._lock:
            if opcode == 0:  # idled: no input for timeout_ms
                self._idle = True
                self._last_input = max(self._last_input, now - self.timeout_ms / 1000)
            elif opcode == 1:  # resumed: input just happened
                self._idle = False
                self._last_input = now

    def _on_dpms(self, obj: int, opcode: int, body: bytes) -> None:
        if opcode != 1:  # mode(u32); supported (0) and done (2) carry nothing needed here
            return
        (mode,) = struct.unpack_from("<I", body, 0)
        self._dpms[obj] = mode
        on = any(m == DPMS_ON for m in self._dpms.values())
        if on != self._screens_on:
            self._screens_on = on
            log.info("screens %s", "on" if on else "off")
            if self.on_screens is not None:
                try:
                    self.on_screens(on)
                except Exception:
                    log.exception("screen power handler failed")
