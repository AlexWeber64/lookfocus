"""Calibration overlay: a borderless, topmost window covering one logical display, with a dot to look at.

Plain Win32 via ctypes (GDI, double-buffered). The window lives on the calling thread; the caller runs
`pump(seconds)` to keep it responsive while it collects samples, so there is no GUI thread to synchronise
with. Sizes scale with the monitor's DPI (the process is per-monitor DPI aware, so rects are physical px).
Esc (a normal WM_KEYDOWN to our own window, not a hook) or closing the window sets `cancelled`.
"""

from __future__ import annotations

import ctypes
import math
import time
from ctypes import wintypes

from lookfocus.displays import Rect

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x00000008, 0x00000080
SW_HIDE, SW_SHOW = 0, 5
HWND_TOPMOST = wintypes.HWND(-1)
SWP_SHOWWINDOW = 0x0040
WM_DESTROY, WM_PAINT, WM_CLOSE, WM_ERASEBKGND, WM_SETCURSOR, WM_KEYDOWN = 0x2, 0xF, 0x10, 0x14, 0x20, 0x100
VK_ESCAPE = 0x1B
PM_REMOVE = 0x1
QS_ALLINPUT = 0x04FF
SRCCOPY = 0x00CC0020
NULL_PEN = 8
TRANSPARENT = 1
DT_CENTER, DT_WORDBREAK, DT_NOPREFIX = 0x1, 0x10, 0x800
FW_NORMAL, FW_SEMIBOLD = 400, 600
CLEARTYPE_QUALITY = 5


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
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
        ("hIconSm", wintypes.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


def _sig(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


H, HDC, INT, UINT = wintypes.HANDLE, wintypes.HDC, ctypes.c_int, wintypes.UINT
_sig(user32, "DefWindowProcW", LRESULT, wintypes.HWND, UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32, "RegisterClassExW", wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(
    user32,
    "CreateWindowExW",
    wintypes.HWND,
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    INT,
    INT,
    INT,
    INT,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
)
_sig(user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
_sig(user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, INT)
_sig(user32, "SetWindowPos", wintypes.BOOL, wintypes.HWND, wintypes.HWND, INT, INT, INT, INT, UINT)
_sig(user32, "SetForegroundWindow", wintypes.BOOL, wintypes.HWND)
_sig(user32, "InvalidateRect", wintypes.BOOL, wintypes.HWND, ctypes.c_void_p, wintypes.BOOL)
_sig(user32, "UpdateWindow", wintypes.BOOL, wintypes.HWND)
_sig(user32, "BeginPaint", HDC, wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
_sig(user32, "EndPaint", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
_sig(user32, "GetClientRect", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_sig(user32, "FillRect", INT, HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH)
_sig(user32, "DrawTextW", INT, HDC, wintypes.LPCWSTR, INT, ctypes.POINTER(wintypes.RECT), UINT)
_sig(user32, "SetCursor", H, H)
_sig(user32, "PeekMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, UINT, UINT, UINT)
_sig(user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_sig(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
_sig(
    user32,
    "MsgWaitForMultipleObjects",
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    wintypes.DWORD,
)
_sig(user32, "GetDpiForWindow", UINT, wintypes.HWND)
_sig(kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
_sig(gdi32, "CreateCompatibleDC", HDC, HDC)
_sig(gdi32, "CreateCompatibleBitmap", H, HDC, INT, INT)
_sig(gdi32, "SelectObject", H, HDC, H)
_sig(gdi32, "DeleteObject", wintypes.BOOL, H)
_sig(gdi32, "DeleteDC", wintypes.BOOL, HDC)
_sig(gdi32, "BitBlt", wintypes.BOOL, HDC, INT, INT, INT, INT, HDC, INT, INT, wintypes.DWORD)
_sig(gdi32, "CreateSolidBrush", wintypes.HBRUSH, wintypes.COLORREF)
_sig(gdi32, "GetStockObject", H, INT)
_sig(gdi32, "Ellipse", wintypes.BOOL, HDC, INT, INT, INT, INT)
_sig(gdi32, "SetTextColor", wintypes.COLORREF, HDC, wintypes.COLORREF)
_sig(gdi32, "SetBkMode", INT, HDC, INT)
_sig(
    gdi32,
    "CreateFontW",
    H,
    INT,
    INT,
    INT,
    INT,
    INT,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPCWSTR,
)


def rgb(r: int, g: int, b: int) -> int:
    return r | (g << 8) | (b << 16)


BG = rgb(18, 18, 22)
TEXT = rgb(225, 225, 230)
DIM = rgb(150, 150, 160)
WAITING = rgb(255, 196, 0)
MEASURING = rgb(60, 220, 110)

_CLASS = "LookFocusCalibrationOverlay"
_class_proc = None  # the registered WNDPROC; must stay referenced for as long as the class exists


class Overlay:
    """One overlay window, reused for each display in turn."""

    def __init__(self) -> None:
        global _class_proc
        self.cancelled = False
        self.dot: tuple[float, float] | None = None
        self.color = WAITING
        self.title = ""
        self.lines: list[str] = []
        self.progress: float | None = None
        self._t0 = time.monotonic()
        hinst = kernel32.GetModuleHandleW(None)
        if _class_proc is None:
            proc = WNDPROC(_bootstrap_proc)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = proc
            wc.hInstance = hinst
            wc.lpszClassName = _CLASS
            if not user32.RegisterClassExW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
            _class_proc = proc
        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            _CLASS,
            "LookFocus calibration",
            WS_POPUP,
            0,
            0,
            100,
            100,
            None,
            None,
            hinst,
            None,
        )
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        _instances[self.hwnd] = self

    # --- API ---
    def show(self, rect: Rect) -> None:
        user32.SetWindowPos(
            self.hwnd, HWND_TOPMOST, rect.left, rect.top, rect.width, rect.height, SWP_SHOWWINDOW
        )
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.SetForegroundWindow(self.hwnd)  # best effort: Esc only works if we get keyboard focus
        self.redraw()

    def set_scene(self, *, dot=None, color=WAITING, title="", lines=(), progress=None) -> None:
        self.dot, self.color, self.title, self.lines, self.progress = dot, color, title, list(lines), progress
        self.redraw()

    def redraw(self) -> None:
        user32.InvalidateRect(self.hwnd, None, False)

    def pump(self, seconds: float) -> bool:
        """Keep the window alive and animated for `seconds`. Returns False once the user has cancelled."""
        end = time.monotonic() + seconds
        msg = wintypes.MSG()
        while not self.cancelled:
            left = end - time.monotonic()
            if left <= 0:
                break
            user32.MsgWaitForMultipleObjects(0, None, False, int(min(left, 0.033) * 1000), QS_ALLINPUT)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if self.dot is not None:
                self.redraw()  # pulse animation
        return not self.cancelled

    def hide(self) -> None:
        user32.ShowWindow(self.hwnd, SW_HIDE)
        self.pump(0.05)

    def close(self) -> None:
        if self.hwnd:
            _instances.pop(self.hwnd, None)
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    # --- window procedure ---
    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            self._paint(hwnd)
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_SETCURSOR:
            user32.SetCursor(None)  # hide the pointer over the overlay so it doesn't pull the eye
            return 1
        if msg == WM_KEYDOWN and wparam == VK_ESCAPE:
            self.cancelled = True
            return 0
        if msg == WM_CLOSE:
            self.cancelled = True
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _font(self, px: int, weight: int = FW_NORMAL):
        return gdi32.CreateFontW(-px, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, CLEARTYPE_QUALITY, 0, "Segoe UI")

    def _paint(self, hwnd) -> None:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        rc = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        w, h = rc.right - rc.left, rc.bottom - rc.top
        s = (user32.GetDpiForWindow(hwnd) or 96) / 96.0
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old_bmp = gdi32.SelectObject(mem, bmp)
        brushes = []
        fonts = []
        try:
            bg = gdi32.CreateSolidBrush(BG)
            brushes.append(bg)
            user32.FillRect(mem, ctypes.byref(rc), bg)
            gdi32.SetBkMode(mem, TRANSPARENT)
            gdi32.SelectObject(mem, gdi32.GetStockObject(NULL_PEN))

            title_font, body_font = self._font(int(26 * s), FW_SEMIBOLD), self._font(int(17 * s))
            fonts += [title_font, body_font]
            box = wintypes.RECT(int(24 * s), int(28 * s), w - int(24 * s), int(28 * s) + int(40 * s))
            gdi32.SelectObject(mem, title_font)
            gdi32.SetTextColor(mem, TEXT)
            user32.DrawTextW(mem, self.title, -1, ctypes.byref(box), DT_CENTER | DT_NOPREFIX)
            gdi32.SelectObject(mem, body_font)
            gdi32.SetTextColor(mem, DIM)
            y = box.bottom + int(6 * s)
            for line in self.lines:
                r = wintypes.RECT(int(24 * s), y, w - int(24 * s), y + int(26 * s))
                user32.DrawTextW(mem, line, -1, ctypes.byref(r), DT_CENTER | DT_NOPREFIX)
                y += int(26 * s)

            if self.dot is not None:
                cx, cy = int(self.dot[0] * (w - 1)), int(self.dot[1] * (h - 1))
                pulse = 0.5 + 0.5 * math.sin((time.monotonic() - self._t0) * 5.0)
                outer = int((16 + 10 * pulse) * s)
                inner = int(5 * s)
                ring = gdi32.CreateSolidBrush(self.color)
                core = gdi32.CreateSolidBrush(rgb(255, 255, 255))
                brushes += [ring, core]
                gdi32.SelectObject(mem, ring)
                gdi32.Ellipse(mem, cx - outer, cy - outer, cx + outer, cy + outer)
                gdi32.SelectObject(mem, core)
                gdi32.Ellipse(mem, cx - inner, cy - inner, cx + inner, cy + inner)
                if self.progress is not None:
                    bar_w = int(120 * s)
                    top = cy + outer + int(12 * s)
                    if top + int(8 * s) > h:
                        top = cy - outer - int(20 * s)
                    left = max(0, min(w - bar_w, cx - bar_w // 2))
                    back = gdi32.CreateSolidBrush(rgb(60, 60, 70))
                    fill = gdi32.CreateSolidBrush(self.color)
                    brushes += [back, fill]
                    user32.FillRect(
                        mem, ctypes.byref(wintypes.RECT(left, top, left + bar_w, top + int(6 * s))), back
                    )
                    done = left + int(bar_w * max(0.0, min(1.0, self.progress)))
                    user32.FillRect(mem, ctypes.byref(wintypes.RECT(left, top, done, top + int(6 * s))), fill)
            gdi32.BitBlt(hdc, 0, 0, w, h, mem, 0, 0, SRCCOPY)
        finally:
            gdi32.SelectObject(mem, old_bmp)
            gdi32.DeleteDC(mem)  # releases the selected font/brush before they are deleted below
            for obj in [*brushes, *fonts, bmp]:
                gdi32.DeleteObject(obj)
            user32.EndPaint(hwnd, ctypes.byref(ps))


_instances: dict[int, Overlay] = {}


def _bootstrap_proc(hwnd, msg, wparam, lparam):
    inst = _instances.get(hwnd)
    if inst is not None:
        return inst._proc(hwnd, msg, wparam, lparam)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
