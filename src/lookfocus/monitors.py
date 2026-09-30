"""Physical monitors and DPI awareness (Windows, ctypes).

enable_dpi_awareness() must run before any window is created so that every coordinate LookFocus uses
(monitor rects, window rects, overlay positions) is in physical pixels on every monitor, whatever its
scaling. list_monitors() uses EnumDisplayMonitors for the rectangles and QueryDisplayConfig for friendly
names and stable device paths.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from lookfocus.displays import Monitor, Rect

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
MONITORINFOF_PRIMARY = 1
MDT_EFFECTIVE_DPI = 0
QDC_ONLY_ACTIVE_PATHS = 2
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", ctypes.c_uint32),
        ("modeInfoIdx", ctypes.c_uint32),
        ("statusFlags", ctypes.c_uint32),
    ]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", ctypes.c_uint32),
        ("modeInfoIdx", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32),
        ("scaling", ctypes.c_uint32),
        ("refreshRate", DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", ctypes.c_uint32),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", ctypes.c_uint32),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", ctypes.c_uint32),
    ]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("infoType", ctypes.c_uint32),
        ("id", ctypes.c_uint32),
        ("adapterId", LUID),
        ("modeInfo", ctypes.c_byte * 48),  # union; unused here
    ]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("adapterId", LUID),
        ("id", ctypes.c_uint32),
    ]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("viewGdiDeviceName", wintypes.WCHAR * 32)]


class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("flags", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32),
        ("edidManufactureId", ctypes.c_uint16),
        ("edidProductCodeId", ctypes.c_uint16),
        ("connectorInstance", ctypes.c_uint32),
        ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
        ("monitorDevicePath", wintypes.WCHAR * 128),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM
)
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), MONITORENUMPROC, wintypes.LPARAM]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetDisplayConfigBufferSizes.argtypes = [
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(ctypes.c_uint32),
]
user32.GetDisplayConfigBufferSizes.restype = wintypes.LONG
user32.QueryDisplayConfig.argtypes = [
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(DISPLAYCONFIG_PATH_INFO),
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.POINTER(DISPLAYCONFIG_MODE_INFO),
    ctypes.c_void_p,
]
user32.QueryDisplayConfig.restype = wintypes.LONG
user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.c_void_p]
user32.DisplayConfigGetDeviceInfo.restype = wintypes.LONG


_dpi_mode: str | None = None


def enable_dpi_awareness() -> str:
    """Make the process per-monitor DPI aware (v2 if possible). Idempotent; returns the mode in effect."""
    global _dpi_mode
    if _dpi_mode is not None:
        return _dpi_mode
    fn = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if fn is not None:
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = wintypes.BOOL
        if fn(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            _dpi_mode = "per-monitor-v2"
            return _dpi_mode
        # ERROR_ACCESS_DENIED: awareness was already set (manifest or an earlier call); report what it is.
        get_ctx = getattr(user32, "GetThreadDpiAwarenessContext", None)
        get_aw = getattr(user32, "GetAwarenessFromDpiAwarenessContext", None)
        if get_ctx and get_aw:
            get_ctx.restype = ctypes.c_void_p
            get_aw.argtypes = [ctypes.c_void_p]
            aw = get_aw(get_ctx())
            _dpi_mode = {0: "unaware", 1: "system", 2: "per-monitor"}.get(aw, f"awareness {aw}")
            return _dpi_mode
    try:
        if ctypes.WinDLL("shcore").SetProcessDpiAwareness(2) == 0:
            _dpi_mode = "per-monitor"
            return _dpi_mode
    except OSError:
        pass
    user32.SetProcessDPIAware()
    _dpi_mode = "system"
    return _dpi_mode


def _display_config_names() -> dict[str, tuple[str, str]]:
    """GDI device name ("\\\\.\\DISPLAY1") -> (friendly name, monitor device path) for active paths."""
    out: dict[str, tuple[str, str]] = {}
    n_paths, n_modes = ctypes.c_uint32(), ctypes.c_uint32()
    if user32.GetDisplayConfigBufferSizes(
        QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes)
    ):
        return out
    paths = (DISPLAYCONFIG_PATH_INFO * n_paths.value)()
    modes = (DISPLAYCONFIG_MODE_INFO * n_modes.value)()
    if user32.QueryDisplayConfig(
        QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths, ctypes.byref(n_modes), modes, None
    ):
        return out
    for p in paths[: n_paths.value]:
        src = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        src.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
        src.header.size = ctypes.sizeof(src)
        src.header.adapterId = p.sourceInfo.adapterId
        src.header.id = p.sourceInfo.id
        tgt = DISPLAYCONFIG_TARGET_DEVICE_NAME()
        tgt.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
        tgt.header.size = ctypes.sizeof(tgt)
        tgt.header.adapterId = p.targetInfo.adapterId
        tgt.header.id = p.targetInfo.id
        if user32.DisplayConfigGetDeviceInfo(ctypes.byref(src)) or user32.DisplayConfigGetDeviceInfo(
            ctypes.byref(tgt)
        ):
            continue
        out[src.viewGdiDeviceName] = (tgt.monitorFriendlyDeviceName, tgt.monitorDevicePath)
    return out


def _dpi_for(hmon) -> int:
    try:
        shcore = ctypes.WinDLL("shcore")
        x, y = ctypes.c_uint(), ctypes.c_uint()
        if shcore.GetDpiForMonitor(hmon, MDT_EFFECTIVE_DPI, ctypes.byref(x), ctypes.byref(y)) == 0:
            return int(x.value)
    except OSError:
        pass
    return 96


def list_monitors() -> list[Monitor]:
    """All active monitors (call enable_dpi_awareness() first, or rects are DPI-virtualised)."""
    handles = []

    @MONITORENUMPROC
    def _cb(hmon, _hdc, _rect, _lparam):
        handles.append(hmon)
        return True

    user32.EnumDisplayMonitors(None, None, _cb, 0)
    try:
        names = _display_config_names()
    except OSError as e:
        log.warning("QueryDisplayConfig failed: %s", e)
        names = {}
    out = []
    for h in handles:
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        if not user32.GetMonitorInfoW(h, ctypes.byref(mi)):
            continue
        r, w = mi.rcMonitor, mi.rcWork
        friendly, path = names.get(mi.szDevice, ("", ""))
        out.append(
            Monitor(
                device=mi.szDevice,
                path=path,
                name=friendly,
                rect=Rect(r.left, r.top, r.right, r.bottom),
                work=Rect(w.left, w.top, w.right, w.bottom),
                primary=bool(mi.dwFlags & MONITORINFOF_PRIMARY),
                dpi=_dpi_for(h),
            )
        )
    return out
