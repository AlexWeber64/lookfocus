"""System events for the tray app: the pause hotkey, session lock, sleep, display power and layout.

A hidden *top-level* window on its own thread (WM_POWERBROADCAST only reaches top-level windows, not
message-only ones) receives:
- WM_HOTKEY from RegisterHotKey (a registered hotkey, not a keyboard hook);
- WM_WTSSESSION_CHANGE lock / unlock (WTSRegisterSessionNotification);
- WM_POWERBROADCAST suspend / resume, and PBT_POWERSETTINGCHANGE for GUID_CONSOLE_DISPLAY_STATE: on Modern
  Standby laptops (like the dev machine) classic suspend notifications may never arrive, but the display
  turning off does;
- WM_DISPLAYCHANGE (monitors added, removed or rearranged).
Each becomes callback(event: str). Also: a single-instance mutex.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import uuid
from collections.abc import Callable
from ctypes import wintypes

from lookfocus.config import parse_hotkey

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY, WM_CLOSE, WM_DISPLAYCHANGE = 0x0002, 0x0010, 0x007E
WM_POWERBROADCAST, WM_WTSSESSION_CHANGE, WM_HOTKEY, WM_APP = 0x0218, 0x02B1, 0x0312, 0x8000
WM_SET_HOTKEY = WM_APP + 1
WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 7, 8
PBT_APMSUSPEND, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC, PBT_POWERSETTINGCHANGE = 4, 7, 0x12, 0x8013
HOTKEY_ID = 1
ERROR_ALREADY_EXISTS = 183


class GUID(ctypes.Structure):
    _fields_ = [
        ("d1", ctypes.c_uint32),
        ("d2", ctypes.c_uint16),
        ("d3", ctypes.c_uint16),
        ("d4", ctypes.c_ubyte * 8),
    ]


GUID_CONSOLE_DISPLAY_STATE = GUID.from_buffer_copy(uuid.UUID("6FE69556-704A-47A0-8F24-C28D936FDA47").bytes_le)


class POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [("PowerSetting", GUID), ("DataLength", wintypes.DWORD), ("Data", ctypes.c_ubyte * 4)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _sig(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


_sig(user32, "DefWindowProcW", LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32, "RegisterClassW", wintypes.ATOM, ctypes.POINTER(WNDCLASSW))
_sig(
    user32,
    "CreateWindowExW",
    wintypes.HWND,
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
)
_sig(user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
_sig(user32, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32, "PostQuitMessage", None, ctypes.c_int)
_sig(
    user32,
    "GetMessageW",
    wintypes.BOOL,
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
)
_sig(user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_sig(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
_sig(user32, "RegisterHotKey", wintypes.BOOL, wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
_sig(user32, "UnregisterHotKey", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_sig(
    user32,
    "RegisterPowerSettingNotification",
    wintypes.HANDLE,
    wintypes.HANDLE,
    ctypes.POINTER(GUID),
    wintypes.DWORD,
)
_sig(user32, "UnregisterPowerSettingNotification", wintypes.BOOL, wintypes.HANDLE)
_sig(wtsapi32, "WTSRegisterSessionNotification", wintypes.BOOL, wintypes.HWND, wintypes.DWORD)
_sig(wtsapi32, "WTSUnRegisterSessionNotification", wintypes.BOOL, wintypes.HWND)
_sig(kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
_sig(kernel32, "CreateMutexW", wintypes.HANDLE, wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)

EventCallback = Callable[[str], None]


class SystemEvents(threading.Thread):
    """Events: "hotkey", "lock", "unlock", "suspend", "resume", "display_off", "display_on",
    "display_change"; and "hotkey_failed:<reason>" if the hotkey can't be registered."""

    _CLASS = "LookFocusSystemEvents"

    def __init__(self, callback: EventCallback, hotkey: str) -> None:
        super().__init__(name="system-events", daemon=True)
        self.callback = callback
        self.hotkey = hotkey
        self.hwnd = None
        self._ready = threading.Event()
        self._proc = WNDPROC(self._wndproc)  # must outlive the window
        self._power = None

    def start(self) -> None:
        super().start()
        self._ready.wait(5)

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        self.join(3)

    def set_hotkey(self, hotkey: str) -> None:
        """Re-register (RegisterHotKey must run on the window's thread, so this posts a message)."""
        self.hotkey = hotkey
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_SET_HOTKEY, 0, 0)

    def _emit(self, event: str) -> None:
        try:
            self.callback(event)
        except Exception:
            log.exception("system event handler failed for %s", event)

    def _register_hotkey(self) -> None:
        user32.UnregisterHotKey(self.hwnd, HOTKEY_ID)
        try:
            mods, vk = parse_hotkey(self.hotkey)
        except ValueError as e:
            self._emit(f"hotkey_failed:{e}")
            return
        if user32.RegisterHotKey(self.hwnd, HOTKEY_ID, mods, vk):
            log.info("pause hotkey %s registered", self.hotkey)
        else:
            err = ctypes.get_last_error()
            reason = "already used by another app" if err == 1409 else f"error {err}"
            log.warning("couldn't register hotkey %s: %s", self.hotkey, reason)
            self._emit(f"hotkey_failed:{self.hotkey} is {reason}")

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_HOTKEY and wparam == HOTKEY_ID:
            self._emit("hotkey")
            return 0
        if msg == WM_SET_HOTKEY:
            self._register_hotkey()
            return 0
        if msg == WM_WTSSESSION_CHANGE:
            if wparam == WTS_SESSION_LOCK:
                self._emit("lock")
            elif wparam == WTS_SESSION_UNLOCK:
                self._emit("unlock")
            return 0
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                self._emit("suspend")
            elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                self._emit("resume")
            elif wparam == PBT_POWERSETTINGCHANGE and lparam:
                s = ctypes.cast(lparam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
                if bytes(s.PowerSetting) == bytes(GUID_CONSOLE_DISPLAY_STATE) and s.DataLength >= 1:
                    self._emit("display_off" if s.Data[0] == 0 else "display_on")  # 1 on, 2 dimmed
            return 1  # TRUE: don't veto anything
        if msg == WM_DISPLAYCHANGE:
            self._emit("display_change")
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = self._CLASS
        user32.RegisterClassW(ctypes.byref(wc))  # fails harmlessly if already registered in this process
        # Hidden top-level window (never shown): broadcasts reach it, the user never sees it.
        self.hwnd = user32.CreateWindowExW(
            0, self._CLASS, "LookFocus", 0, 0, 0, 0, 0, None, None, hinst, None
        )
        if not self.hwnd:
            log.error("couldn't create the system-events window (%d)", ctypes.get_last_error())
            self._ready.set()
            return
        if not wtsapi32.WTSRegisterSessionNotification(self.hwnd, 0):  # NOTIFY_FOR_THIS_SESSION
            log.warning("session lock notifications unavailable (%d)", ctypes.get_last_error())
        self._power = user32.RegisterPowerSettingNotification(
            self.hwnd, ctypes.byref(GUID_CONSOLE_DISPLAY_STATE), 0
        )
        self._register_hotkey()
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(self.hwnd, HOTKEY_ID)
            wtsapi32.WTSUnRegisterSessionNotification(self.hwnd)
            if self._power:
                user32.UnregisterPowerSettingNotification(self._power)
            self.hwnd = None


_mutex = None


def claim_single_instance(name: str = "Local\\LookFocus.SingleInstance") -> bool:
    """True if this is the only running LookFocus tray app (the mutex lives as long as the process)."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, name)
    return bool(_mutex) and ctypes.get_last_error() != ERROR_ALREADY_EXISTS
