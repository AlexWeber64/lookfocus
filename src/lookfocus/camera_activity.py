"""Which processes have a camera open, per device: Media Foundation's sensor activity monitor.

`MFCreateSensorActivityMonitor` (mfsensorgroup.dll, Windows 10 1809+) calls back whenever camera usage
changes, with, per device (symbolic link), every process that has it open, whether it is streaming,
and whether it is the controlling client or a shared one. This is the mechanism behind Windows' own
camera privacy indicator. Unlike the camera privacy store (camera_usage.py), it reports per *process* and
per *device*, and it reports processes that opened the camera but aren't streaming. That's exactly how a
second app looks while LookFocus holds the camera and blocks it (DECISIONS.md D-023).

Pure ctypes COM; the callback object is implemented here. Interface IDs and vtable order come from the
Windows SDK's mfidl.h (10.0.26100.0).

Run as a module to print live activity:  python -m lookfocus.camera_activity
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
import uuid
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

HRESULT = ctypes.c_long
S_OK, E_NOINTERFACE, E_POINTER = 0, -2147467262, -2147467261  # 0x80004002, 0x80004003
MF_VERSION, MFSTARTUP_LITE = 0x00020070, 1
COINIT_MULTITHREADED = 0


class GUID(ctypes.Structure):
    _fields_ = [
        ("d1", ctypes.c_uint32),
        ("d2", ctypes.c_uint16),
        ("d3", ctypes.c_uint16),
        ("d4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def of(cls, s: str) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(s).bytes_le)

    def key(self) -> bytes:
        return bytes(self)


IID_IUnknown = GUID.of("00000000-0000-0000-C000-000000000046")
IID_IAgileObject = GUID.of("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")
IID_IMFSensorActivitiesReportCallback = GUID.of("DE5072EE-DBE3-46DC-8A87-B6F631194751")
_ACCEPTED_IIDS = {g.key() for g in (IID_IUnknown, IID_IAgileObject, IID_IMFSensorActivitiesReportCallback)}

# vtable slots (IUnknown = 0..2)
_RELEASE = 2
_REPORTS_GET_COUNT, _REPORTS_GET = 3, 4  # IMFSensorActivitiesReport
_DEV_NAME, _DEV_LINK, _DEV_PCOUNT, _DEV_PROC = 3, 4, 5, 6  # IMFSensorActivityReport
_PROC_PID, _PROC_STREAMING, _PROC_MODE, _PROC_TIME = 3, 4, 5, 6  # IMFSensorProcessActivity
_MON_START, _MON_STOP = 3, 4  # IMFSensorActivityMonitor


@dataclass(frozen=True)
class SensorProcess:
    pid: int
    streaming: bool
    mode: str  # "controller" or "shared"


@dataclass(frozen=True)
class SensorDevice:
    name: str
    link: str
    processes: tuple[SensorProcess, ...]


def _call(obj, slot, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return lambda *a: ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)(vtbl[slot])(obj, *a)


def _release(obj) -> None:
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[_RELEASE])(obj)


def _parse(report) -> list[SensorDevice]:
    out = []
    n = ctypes.c_ulong()
    if _call(report, _REPORTS_GET_COUNT, ctypes.POINTER(ctypes.c_ulong))(ctypes.byref(n)) < 0:
        return out
    for i in range(n.value):
        dev = ctypes.c_void_p()
        if (
            _call(report, _REPORTS_GET, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))(i, ctypes.byref(dev))
            < 0
        ):
            continue
        try:

            def text(slot, dev=dev, size=1024) -> str:
                buf = ctypes.create_unicode_buffer(size)
                written = ctypes.c_ulong()
                fn = _call(dev, slot, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong))
                return buf.value if fn(buf, size, ctypes.byref(written)) >= 0 else ""

            name, link = text(_DEV_NAME), text(_DEV_LINK)
            pc = ctypes.c_ulong()
            _call(dev, _DEV_PCOUNT, ctypes.POINTER(ctypes.c_ulong))(ctypes.byref(pc))
            procs = []
            for j in range(pc.value):
                pa = ctypes.c_void_p()
                get = _call(dev, _DEV_PROC, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))
                if get(j, ctypes.byref(pa)) < 0:
                    continue
                try:
                    pid, streaming, mode = ctypes.c_ulong(), wintypes.BOOL(), ctypes.c_int()
                    _call(pa, _PROC_PID, ctypes.POINTER(ctypes.c_ulong))(ctypes.byref(pid))
                    _call(pa, _PROC_STREAMING, ctypes.POINTER(wintypes.BOOL))(ctypes.byref(streaming))
                    _call(pa, _PROC_MODE, ctypes.POINTER(ctypes.c_int))(ctypes.byref(mode))
                    procs.append(
                        SensorProcess(
                            int(pid.value),
                            bool(streaming.value),
                            "controller" if mode.value == 0 else "shared",
                        )
                    )
                finally:
                    _release(pa)
            out.append(SensorDevice(name, link, tuple(procs)))
        finally:
            _release(dev)
    return out


# --- the COM callback object -------------------------------------------------------------------

_QI = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
_ADDREF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_REL = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_ONREPORT = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p)


class _Vtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI),
        ("AddRef", _ADDREF),
        ("Release", _REL),
        ("OnActivitiesReport", _ONREPORT),
    ]


class _Obj(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_Vtbl))]


class _Callback:
    """IMFSensorActivitiesReportCallback implemented in Python. Lifetime is owned by the monitor wrapper,
    which keeps this object alive until the monitor is stopped and released; COM refcounts are tracked
    but never free anything."""

    def __init__(self, on_report: Callable[[list[SensorDevice]], None]) -> None:
        self.on_report = on_report
        self.refs = 1
        self._vtbl = _Vtbl(_QI(self._qi), _ADDREF(self._addref), _REL(self._release), _ONREPORT(self._report))
        self.obj = _Obj(ctypes.pointer(self._vtbl))
        self.ptr = ctypes.cast(ctypes.pointer(self.obj), ctypes.c_void_p)

    def _qi(self, this, riid, ppv):
        if not ppv:
            return E_POINTER
        if riid and riid.contents.key() in _ACCEPTED_IIDS:
            ppv[0] = this
            self.refs += 1
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _addref(self, this):
        self.refs += 1
        return self.refs

    def _release(self, this):
        self.refs = max(0, self.refs - 1)
        return self.refs

    def _report(self, this, report):
        try:
            self.on_report(_parse(report))
        except Exception:
            log.exception("sensor activity report handling failed")
        return S_OK


class SensorActivityMonitor(threading.Thread):
    """Runs the MF sensor activity monitor; `callback(devices)` is called (from an MF worker thread) on
    every change, with the full current picture."""

    def __init__(self, callback: Callable[[list[SensorDevice]], None]) -> None:
        super().__init__(name="sensor-activity", daemon=True)
        self._cb = _Callback(callback)
        self._stop_evt = threading.Event()
        self.started_ok = threading.Event()
        self.error: str = ""

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_evt.set()
        self.join(timeout)

    def run(self) -> None:
        ole32 = ctypes.WinDLL("ole32")
        mfplat = ctypes.WinDLL("mfplat")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = HRESULT
        mfplat.MFStartup.argtypes = [wintypes.ULONG, wintypes.DWORD]
        mfplat.MFStartup.restype = HRESULT
        hr_co = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        mf_ok = mfplat.MFStartup(MF_VERSION, MFSTARTUP_LITE) >= 0
        mon = ctypes.c_void_p()
        try:
            try:
                lib = ctypes.WinDLL("mfsensorgroup")
                create = lib.MFCreateSensorActivityMonitor
            except (OSError, AttributeError) as e:
                self.error = f"sensor activity monitor unavailable: {e}"
                return
            create.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            create.restype = HRESULT
            hr = create(self._cb.ptr, ctypes.byref(mon))
            if hr < 0 or not mon:
                self.error = f"MFCreateSensorActivityMonitor failed: 0x{hr & 0xFFFFFFFF:08x}"
                return
            hr = _call(mon, _MON_START)()
            if hr < 0:
                self.error = f"IMFSensorActivityMonitor::Start failed: 0x{hr & 0xFFFFFFFF:08x}"
                return
            self.started_ok.set()
            self._stop_evt.wait()
            _call(mon, _MON_STOP)()
        finally:
            if self.error:
                log.warning(self.error)
            _release(mon)
            if mf_ok:
                mfplat.MFShutdown()
            if hr_co >= 0:
                ole32.CoUninitialize()
            self.started_ok.set()


def process_name(pid: int) -> str:
    """Executable base name of a process (without .exe), or "" if it can't be read."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        exe = buf.value.rsplit("\\", 1)[-1]
        return exe[:-4] if exe.lower().endswith(".exe") else exe
    finally:
        k32.CloseHandle(h)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    t0 = time.monotonic()

    def show(devs: list[SensorDevice]) -> None:
        stamp = f"{time.monotonic() - t0:7.2f}s"
        if not devs:
            print(f"{stamp} no camera in use", flush=True)
        for d in devs:
            procs = (
                ", ".join(
                    f"{process_name(p.pid) or '?'}({p.pid}) "
                    f"{'STREAMING' if p.streaming else 'open, not streaming'} {p.mode}"
                    for p in d.processes
                )
                or "no processes"
            )
            print(f"{stamp} {d.name}: {procs}", flush=True)

    mon = SensorActivityMonitor(show)
    mon.start()
    mon.started_ok.wait(5)
    if mon.error:
        print(mon.error)
        return
    print("watching camera activity; Ctrl+C to stop")
    try:
        while mon.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    mon.stop()


if __name__ == "__main__":
    main()
