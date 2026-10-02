"""Win32 side of focus switching: window enumeration and filtering, the foreground tracker, the real
FocusApi for targeting.activate(), and elevation (UIPI) helpers.

Coordinates are physical pixels (the process is per-monitor DPI aware; see monitors.py).
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

from lookfocus.camera_activity import process_name
from lookfocus.displays import LogicalDisplay, Rect
from lookfocus.targeting import Activation, WindowInfo, activate, choose_target, display_of

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
shell32 = ctypes.WinDLL("shell32")

HWND, BOOL, DWORD, UINT, LONG = wintypes.HWND, wintypes.BOOL, wintypes.DWORD, wintypes.UINT, wintypes.LONG
ULONG_PTR = ctypes.c_size_t
LONG_PTR = ctypes.c_ssize_t

GA_ROOTOWNER = 3
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CHILD = 0x40000000
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x00040000, 0x08000000
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
SW_MINIMIZE, SW_RESTORE = 6, 9
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
INPUT_KEYBOARD, KEYEVENTF_KEYUP = 1, 0x0002
SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
PROCESS_QUERY_LIMITED_INFORMATION, TOKEN_QUERY, TOKEN_ELEVATION_CLASS = 0x1000, 0x0008, 20
VK_LBUTTON, VK_RBUTTON, VK_MBUTTON = 0x01, 0x02, 0x04

# Shell / system surfaces that are never focus targets even when visible and foreground.
SHELL_CLASSES = frozenset(
    {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "NotifyIconOverflowWindow",
        # Start, Search and shell flyouts (UWP apps live in ApplicationFrameWindow instead):
        "Windows.UI.Core.CoreWindow",
        "XamlExplorerHostIslandWindow",
        "TopLevelWindowForOverflowXamlIsland",
        "ForegroundStaging",
        "MultitaskingViewFrame",
        "TaskSwitcherWnd",
        "Shell_InputSwitchTopLevelWindow",
        "LockScreenControllerProxyWindow",
    }
)


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", UINT),
        ("flags", UINT),
        ("showCmd", UINT),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("flags", DWORD),
        ("hwndActive", HWND),
        ("hwndFocus", HWND),
        ("hwndCapture", HWND),
        ("hwndMenuOwner", HWND),
        ("hwndMoveSize", HWND),
        ("hwndCaret", HWND),
        ("rcCaret", RECT),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


def _sig(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)
GetForegroundWindow = _sig(user32, "GetForegroundWindow", HWND)
SetForegroundWindow = _sig(user32, "SetForegroundWindow", BOOL, HWND)
BringWindowToTop = _sig(user32, "BringWindowToTop", BOOL, HWND)
ShowWindow = _sig(user32, "ShowWindow", BOOL, HWND, ctypes.c_int)
IsIconic = _sig(user32, "IsIconic", BOOL, HWND)
IsWindow = _sig(user32, "IsWindow", BOOL, HWND)
IsWindowVisible = _sig(user32, "IsWindowVisible", BOOL, HWND)
IsHungAppWindow = _sig(user32, "IsHungAppWindow", BOOL, HWND)
GetWindowThreadProcessId = _sig(user32, "GetWindowThreadProcessId", DWORD, HWND, ctypes.POINTER(DWORD))
AttachThreadInput = _sig(user32, "AttachThreadInput", BOOL, DWORD, DWORD, BOOL)
GetCurrentThreadId = _sig(kernel32, "GetCurrentThreadId", DWORD)
GetAncestor = _sig(user32, "GetAncestor", HWND, HWND, UINT)
GetLastActivePopup = _sig(user32, "GetLastActivePopup", HWND, HWND)
GetWindowTextLengthW = _sig(user32, "GetWindowTextLengthW", ctypes.c_int, HWND)
GetWindowTextW = _sig(user32, "GetWindowTextW", ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
GetClassNameW = _sig(user32, "GetClassNameW", ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int)
GetWindowLongPtrW = _sig(user32, "GetWindowLongPtrW", LONG_PTR, HWND, ctypes.c_int)
EnumWindows = _sig(user32, "EnumWindows", BOOL, WNDENUMPROC, wintypes.LPARAM)
GetShellWindow = _sig(user32, "GetShellWindow", HWND)
GetDesktopWindow = _sig(user32, "GetDesktopWindow", HWND)
SendInput = _sig(user32, "SendInput", UINT, UINT, ctypes.POINTER(INPUT), ctypes.c_int)
SetWindowPos = _sig(
    user32, "SetWindowPos", BOOL, HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, UINT
)
GetWindowPlacement = _sig(user32, "GetWindowPlacement", BOOL, HWND, ctypes.POINTER(WINDOWPLACEMENT))
GetWindowRect = _sig(user32, "GetWindowRect", BOOL, HWND, ctypes.POINTER(RECT))
GetGUIThreadInfo = _sig(user32, "GetGUIThreadInfo", BOOL, DWORD, ctypes.POINTER(GUITHREADINFO))
GetCursorPos = _sig(user32, "GetCursorPos", BOOL, ctypes.POINTER(POINT))
SetCursorPos = _sig(user32, "SetCursorPos", BOOL, ctypes.c_int, ctypes.c_int)
GetAsyncKeyState = _sig(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
SystemParametersInfoW = _sig(user32, "SystemParametersInfoW", BOOL, UINT, UINT, ctypes.c_void_p, UINT)
DwmGetWindowAttribute = _sig(
    dwmapi, "DwmGetWindowAttribute", ctypes.c_long, HWND, DWORD, ctypes.c_void_p, DWORD
)
OpenProcess = _sig(kernel32, "OpenProcess", wintypes.HANDLE, DWORD, BOOL, DWORD)
CloseHandle = _sig(kernel32, "CloseHandle", BOOL, wintypes.HANDLE)
GetCurrentProcess = _sig(kernel32, "GetCurrentProcess", wintypes.HANDLE)
OpenProcessToken = _sig(
    advapi32, "OpenProcessToken", BOOL, wintypes.HANDLE, DWORD, ctypes.POINTER(wintypes.HANDLE)
)
GetTokenInformation = _sig(
    advapi32,
    "GetTokenInformation",
    BOOL,
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    DWORD,
    ctypes.POINTER(DWORD),
)
ShellExecuteW = _sig(
    shell32,
    "ShellExecuteW",
    wintypes.HINSTANCE,
    HWND,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_int,
)


# ------------------------------------------------------------------------------------------------
# window facts
# ------------------------------------------------------------------------------------------------


def _text(fn, hwnd, size: int = 256) -> str:
    buf = ctypes.create_unicode_buffer(size)
    fn(hwnd, buf, size)
    return buf.value


def title_of(hwnd) -> str:
    n = GetWindowTextLengthW(hwnd)
    return _text(GetWindowTextW, hwnd, n + 1) if n > 0 else ""


def class_of(hwnd) -> str:
    return _text(GetClassNameW, hwnd, 256)


def pid_tid(hwnd) -> tuple[int, int]:
    pid = DWORD()
    tid = GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value), int(tid)


def is_cloaked(hwnd) -> bool:
    """Hidden by DWM: suspended UWP apps, windows on other virtual desktops, shell surfaces."""
    v = DWORD()
    return DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) == 0 and v.value != 0


def frame_rect(hwnd) -> Rect:
    """Visible frame (GetWindowRect includes the invisible resize borders on Windows 10+)."""
    r = RECT()
    if DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        GetWindowRect(hwnd, ctypes.byref(r))
    return Rect(r.left, r.top, r.right, r.bottom)


def normal_rect(hwnd) -> Rect:
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(wp)
    GetWindowPlacement(hwnd, ctypes.byref(wp))
    r = wp.rcNormalPosition
    return Rect(r.left, r.top, r.right, r.bottom)


def root_owner(hwnd) -> int:
    return GetAncestor(hwnd, GA_ROOTOWNER) or hwnd if hwnd else 0


def process_elevated(pid: int) -> bool | None:
    """True/False from the process token; None if the token can't be read (typically a higher-integrity,
    i.e. elevated, process seen from a non-elevated one)."""
    if pid == os.getpid():
        h, close = GetCurrentProcess(), False
    else:
        h, close = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid), True
        if not h:
            return None
    tok = wintypes.HANDLE()
    try:
        if not OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            elevated, n = DWORD(), DWORD()
            if not GetTokenInformation(
                tok, TOKEN_ELEVATION_CLASS, ctypes.byref(elevated), 4, ctypes.byref(n)
            ):
                return None
            return bool(elevated.value)
        finally:
            CloseHandle(tok)
    finally:
        if close:
            CloseHandle(h)


def we_are_elevated() -> bool:
    return bool(process_elevated(os.getpid()))


def window_info(hwnd) -> WindowInfo:
    pid, _ = pid_tid(hwnd)
    minimized = bool(IsIconic(hwnd))
    return WindowInfo(
        hwnd=int(hwnd),
        title=title_of(hwnd),
        cls=class_of(hwnd),
        pid=pid,
        exe=process_name(pid),
        rect=normal_rect(hwnd) if minimized else frame_rect(hwnd),
        minimized=minimized,
        elevated=process_elevated(pid),
    )


def is_candidate(hwnd, own_pid: int | None = None) -> bool:
    """Alt-Tab-style eligibility: a visible, un-cloaked, activatable top-level app window that isn't a
    shell surface or one of LookFocus's own windows."""
    if not hwnd or not IsWindow(hwnd) or not IsWindowVisible(hwnd) or is_cloaked(hwnd):
        return False
    if hwnd in (GetShellWindow(), GetDesktopWindow()):
        return False
    pid, _ = pid_tid(hwnd)
    if pid == (os.getpid() if own_pid is None else own_pid):
        return False
    style = GetWindowLongPtrW(hwnd, GWL_STYLE)
    ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if style & WS_CHILD or ex & WS_EX_NOACTIVATE:  # disabled owners (modal dialog open) stay eligible
        return False
    if ex & WS_EX_TOOLWINDOW and not ex & WS_EX_APPWINDOW:
        return False
    if root_owner(hwnd) != hwnd and not ex & WS_EX_APPWINDOW:
        return False  # owned popups are reached through their owner (GetLastActivePopup at activation)
    if class_of(hwnd) in SHELL_CLASSES:
        return False
    r = frame_rect(hwnd)
    return IsIconic(hwnd) or (r.width > 1 and r.height > 1)


def candidate_windows(own_pid: int | None = None) -> list[WindowInfo]:
    """Eligible top-level windows, topmost first (EnumWindows enumerates in z-order)."""
    hwnds: list[int] = []

    @WNDENUMPROC
    def cb(hwnd, _lparam):
        hwnds.append(hwnd)
        return True

    EnumWindows(cb, 0)
    return [window_info(h) for h in hwnds if is_candidate(h, own_pid)]


def foreground_lock_timeout_ms() -> int:
    v = DWORD()
    SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(v), 0)
    return int(v.value)


# ------------------------------------------------------------------------------------------------
# activation
# ------------------------------------------------------------------------------------------------


class Win32FocusApi:
    """Real primitives for targeting.activate()."""

    def foreground(self) -> int:
        return GetForegroundWindow() or 0

    def same_window(self, a: int, b: int) -> bool:
        return bool(a) and bool(b) and root_owner(a) == root_owner(b)

    def is_iconic(self, hwnd: int) -> bool:
        return bool(IsIconic(hwnd))

    def restore(self, hwnd: int) -> None:
        ShowWindow(hwnd, SW_RESTORE)

    def set_foreground(self, hwnd: int) -> bool:
        return bool(SetForegroundWindow(hwnd))

    def attach_and_activate(self, hwnd: int) -> str:
        """Attach this thread's input to the foreground thread and the target's, bring the target up,
        detach in all cases."""
        me = GetCurrentThreadId()
        fg = GetForegroundWindow()
        fg_tid = pid_tid(fg)[1] if fg else 0
        tgt_tid = pid_tid(hwnd)[1]
        attached = []
        notes = []
        try:
            for tid in dict.fromkeys((fg_tid, tgt_tid)):
                if tid and tid != me:
                    if AttachThreadInput(me, tid, True):
                        attached.append(tid)
                    else:
                        notes.append(f"attach to {tid} failed ({ctypes.get_last_error()})")
            BringWindowToTop(hwnd)
            ok = SetForegroundWindow(hwnd)
            notes.append(f"set_foreground={bool(ok)}")
        finally:
            for tid in attached:
                AttachThreadInput(me, tid, False)
        return "; ".join(notes)

    def tap_key(self, vk: int) -> bool:
        inputs = (INPUT * 2)()
        for i, flags in enumerate((0, KEYEVENTF_KEYUP)):
            inputs[i].type = INPUT_KEYBOARD
            inputs[i].u.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
        return SendInput(2, inputs, ctypes.sizeof(INPUT)) == 2

    def now(self) -> float:
        return time.perf_counter()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_API = Win32FocusApi()


def activation_target(hwnd: int) -> int:
    """The window to actually activate: the root owner's last active popup if it is a visible dialog
    (a modal Save dialog, say), else the root owner itself."""
    root = root_owner(hwnd)
    popup = GetLastActivePopup(root)
    if popup and popup != root and IsWindowVisible(popup) and not is_cloaked(popup):
        return int(popup)
    return int(root)


def focus_window(
    hwnd: int, *, allow_alt: bool = True, first_stage: str = "restore", stage_timeout: float = 0.5
) -> Activation:
    """Bring `hwnd` (or its active popup) to the foreground; explains UIPI failures."""
    target = activation_target(hwnd)
    if not IsWindow(target):
        return Activation(False, None, reason="window no longer exists")
    if IsHungAppWindow(target):
        return Activation(False, None, reason="window is not responding")
    act = activate(_API, target, allow_alt=allow_alt, first_stage=first_stage, stage_timeout=stage_timeout)
    if act.ok:
        log.info("focused %s via %s (%.0f ms)", window_info(target).label, act.stage, act.total_ms)
        return act
    pid, _ = pid_tid(target)
    elevated = process_elevated(pid)
    if elevated is not False and not we_are_elevated():
        act.reason = (
            "the window belongs to an elevated (administrator) process; Windows blocks non-elevated apps "
            "from activating it (UIPI). Run LookFocus with --elevated to allow this."
        )
    else:
        fg = GetForegroundWindow()
        fg_pid = pid_tid(fg)[0] if fg else 0
        if fg and process_elevated(fg_pid) is not False and not we_are_elevated():
            act.reason = (
                "the current foreground window belongs to an elevated process; Windows blocks non-elevated "
                "apps from taking focus from it (UIPI). Run LookFocus with --elevated to allow this."
            )
    log.warning("couldn't focus %s: %s", window_info(target).label, act.reason)
    return act


def keyboard_focus_in(hwnd: int) -> bool:
    """Is keyboard input going to this window now? Asks about the *foreground* thread (tid 0): for classic
    console windows (ConsoleWindowClass) the window's own thread reports no focus even when it has it
    (measured, D-029)."""
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(gti)
    if not GetGUIThreadInfo(0, ctypes.byref(gti)):
        return False
    root = root_owner(hwnd)
    return any(h and root_owner(h) == root for h in (gti.hwndFocus, gti.hwndActive))


def move_cursor_into(rect: Rect) -> None:
    x, y = rect.center
    SetCursorPos(int(x), int(y))


def cursor_pos() -> tuple[int, int]:
    p = POINT()
    GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def mouse_buttons_down() -> bool:
    """Mouse buttons only: LookFocus never reads keyboard key state."""
    return any(GetAsyncKeyState(vk) & 0x8000 for vk in (VK_LBUTTON, VK_RBUTTON, VK_MBUTTON))


def place_window(hwnd: int, rect: Rect) -> None:
    """Restore if minimized/maximized and move into rect (used by tools to arrange test windows)."""
    ShowWindow(hwnd, SW_RESTORE)
    SetWindowPos(hwnd, None, rect.left, rect.top, rect.width, rect.height, SWP_NOZORDER | SWP_NOACTIVATE)


def minimize(hwnd: int) -> None:
    ShowWindow(hwnd, SW_MINIMIZE)


def relaunch_elevated(argv: list[str]) -> bool:
    """Start this program again as administrator (UAC prompt). Opt-in only (--elevated)."""
    params = subprocess.list2cmdline(argv)
    rc = ShellExecuteW(None, "runas", sys.executable, params, os.getcwd(), 1)
    return int(rc or 0) > 32


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


ShellExecuteExW = _sig(shell32, "ShellExecuteExW", BOOL, ctypes.POINTER(SHELLEXECUTEINFOW))
WaitForSingleObject = _sig(kernel32, "WaitForSingleObject", DWORD, wintypes.HANDLE, DWORD)
SEE_MASK_NOCLOSEPROCESS = 0x00000040


def start_elevated(exe: str, argv: list[str], show: int = 0) -> int | None:
    """Run exe as administrator (UAC prompt); returns a process handle to wait on, or None if refused."""
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb, sei.lpFile = "runas", exe
    sei.lpParameters, sei.lpDirectory = subprocess.list2cmdline(argv), os.getcwd()
    sei.nShow = show
    if not ShellExecuteExW(ctypes.byref(sei)) or not sei.hProcess:
        return None
    return sei.hProcess


def process_running(handle: int) -> bool:
    return WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT


# ------------------------------------------------------------------------------------------------
# remembering the last active window per display
# ------------------------------------------------------------------------------------------------


class ForegroundTracker(threading.Thread):
    """Polls the foreground window (~4 Hz) and remembers the last active eligible window per display."""

    def __init__(self, displays: Callable[[], list[LogicalDisplay]], interval: float = 0.25) -> None:
        super().__init__(name="foreground-tracker", daemon=True)
        self.displays = displays
        self.interval = interval
        self._last: dict[str, int] = {}
        self._lock = threading.Lock()
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()
        self.join(2)

    def remembered(self, display_id: str) -> int | None:
        with self._lock:
            return self._last.get(display_id)

    def note(self, hwnd: int) -> None:
        """Record hwnd as active (also used right after LookFocus focuses something)."""
        root = root_owner(hwnd)
        if not is_candidate(root):
            return
        d = display_of(window_info(root), self.displays())
        if d is not None:
            with self._lock:
                if self._last.get(d.id) != root:
                    log.debug("last active on %s: %s", d.short, window_info(root).label)
                self._last[d.id] = root

    def target_for(self, display: LogicalDisplay) -> WindowInfo | None:
        return choose_target(display, self.remembered(display.id), candidate_windows(), self.displays())

    def run(self) -> None:
        last = 0
        while not self._halt.is_set():
            try:
                fg = GetForegroundWindow() or 0
                if fg != last:
                    last = fg
                    self.note(fg)
            except Exception:
                log.exception("foreground tracking failed")
            self._halt.wait(self.interval)
