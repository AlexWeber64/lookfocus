"""Enumerate video capture devices on Windows, in the order OpenCV indexes them.

OpenCV addresses cameras by integer index only. Its MSMF backend indexes the list returned by
MFEnumDeviceSources(VIDCAP), and its DSHOW backend indexes the DirectShow
CLSID_VideoInputDeviceCategory enumeration. Walking those two lists here gives each OpenCV index
a friendly name and a stable device path (so config can survive index reshuffles).

Pure ctypes COM (vtable calls); runs on a private MTA thread so it never disturbs the caller's
COM apartment. Windows-only.
"""

from __future__ import annotations

import ctypes
import re
import threading
import uuid
from ctypes import wintypes
from dataclasses import dataclass


@dataclass(frozen=True)
class CameraDevice:
    backend: str  # "msmf" or "dshow"
    index: int  # OpenCV index for that backend
    name: str
    path: str  # MF symbolic link / DirectShow DevicePath ("" if the driver exposes none)

    @property
    def looks_ir(self) -> bool:
        """Name-based hint only; frame content (see capture.frame_stats) is the real test."""
        return bool(re.search(r"\bIR\b|infra ?red|hello", self.name, re.I))


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def of(cls, s: str) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(s).bytes_le)


class VARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("r1", ctypes.c_ushort),
        ("r2", ctypes.c_ushort),
        ("r3", ctypes.c_ushort),
        ("ptr", ctypes.c_void_p),
        ("ptr2", ctypes.c_void_p),
    ]


HRESULT = ctypes.c_long
VT_BSTR = 8
CLSCTX_INPROC_SERVER = 1
COINIT_MULTITHREADED = 0
MF_VERSION = 0x00020070
MFSTARTUP_LITE = 1

MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE = GUID.of("c60ac5fe-252a-478f-a0ef-bc8fa5f7cad3")
MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID = GUID.of("8ac3587a-4ae7-42d8-99e0-0a6013eef90f")
MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME = GUID.of("60d0e559-52f8-4fa2-bbce-acdb34a8ec01")
MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK = GUID.of("58f0aad8-22bf-4f8a-bb3d-d2c4978c6e2f")

CLSID_SystemDeviceEnum = GUID.of("62BE5D10-60EB-11d0-BD3B-00A0C911CE86")
IID_ICreateDevEnum = GUID.of("29840822-5B84-11D0-BD3B-00A0C911CE86")
CLSID_VideoInputDeviceCategory = GUID.of("860BB310-5D01-11d0-BD3B-00A0C911CE86")
IID_IPropertyBag = GUID.of("55272A00-42CB-11CE-8135-00AA004BB851")

# vtable slots
_RELEASE = 2
_ATTR_GET_ALLOCATED_STRING = 13  # IMFAttributes
_ATTR_SET_GUID = 24  # IMFAttributes
_DEVENUM_CREATE_CLASS_ENUMERATOR = 3  # ICreateDevEnum
_ENUMMONIKER_NEXT = 3  # IEnumMoniker
_MONIKER_BIND_TO_STORAGE = 9  # IMoniker (IUnknown 3 + IPersist 1 + IPersistStream 4 + BindToObject)
_PROPBAG_READ = 3  # IPropertyBag


def _vcall(obj: ctypes.c_void_p, slot: int, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)
    fn = proto(vtbl[slot])
    return lambda *args: fn(obj, *args)


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[_RELEASE])(obj)


def _ole32():
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = HRESULT
    return ole32


def _enum_msmf() -> list[CameraDevice]:
    ole32 = _ole32()
    mfplat = ctypes.WinDLL("mfplat")
    mf = ctypes.WinDLL("mf")
    mfplat.MFStartup.argtypes = [wintypes.ULONG, wintypes.DWORD]
    mfplat.MFStartup.restype = HRESULT
    mfplat.MFShutdown.restype = HRESULT
    mfplat.MFCreateAttributes.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
    mfplat.MFCreateAttributes.restype = HRESULT
    mf.MFEnumDeviceSources.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    mf.MFEnumDeviceSources.restype = HRESULT

    if mfplat.MFStartup(MF_VERSION, MFSTARTUP_LITE) < 0:
        return []
    out: list[CameraDevice] = []
    attrs = ctypes.c_void_p()
    devices = ctypes.POINTER(ctypes.c_void_p)()
    count = ctypes.c_uint32(0)
    try:
        if mfplat.MFCreateAttributes(ctypes.byref(attrs), 1) < 0:
            return []
        set_guid = _vcall(attrs, _ATTR_SET_GUID, ctypes.POINTER(GUID), ctypes.POINTER(GUID))
        if (
            set_guid(
                ctypes.byref(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE),
                ctypes.byref(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID),
            )
            < 0
        ):
            return []
        if mf.MFEnumDeviceSources(attrs, ctypes.byref(devices), ctypes.byref(count)) < 0:
            return []
        for i in range(count.value):
            act = ctypes.c_void_p(devices[i])

            def get_str(key: GUID, act=act) -> str:
                getter = _vcall(
                    act,
                    _ATTR_GET_ALLOCATED_STRING,
                    ctypes.POINTER(GUID),
                    ctypes.POINTER(ctypes.c_wchar_p),
                    ctypes.POINTER(ctypes.c_uint32),
                )
                p = ctypes.c_wchar_p()
                n = ctypes.c_uint32()
                if getter(ctypes.byref(key), ctypes.byref(p), ctypes.byref(n)) < 0:
                    return ""
                s = p.value or ""
                ole32.CoTaskMemFree(p)
                return s

            out.append(
                CameraDevice(
                    "msmf",
                    i,
                    get_str(MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME),
                    get_str(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK),
                )
            )
            _release(act)
    finally:
        if devices:
            ole32.CoTaskMemFree(devices)
        _release(attrs)
        mfplat.MFShutdown()
    return out


def _enum_dshow() -> list[CameraDevice]:
    ole32 = _ole32()
    oleaut32 = ctypes.WinDLL("oleaut32")
    oleaut32.VariantClear.argtypes = [ctypes.POINTER(VARIANT)]
    out: list[CameraDevice] = []
    devenum = ctypes.c_void_p()
    if (
        ole32.CoCreateInstance(
            ctypes.byref(CLSID_SystemDeviceEnum),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_ICreateDevEnum),
            ctypes.byref(devenum),
        )
        < 0
    ):
        return []
    enum_mk = ctypes.c_void_p()
    try:
        create = _vcall(
            devenum,
            _DEVENUM_CREATE_CLASS_ENUMERATOR,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        )
        hr = create(ctypes.byref(CLSID_VideoInputDeviceCategory), ctypes.byref(enum_mk), 0)
        if hr != 0 or not enum_mk:  # S_FALSE (1) means the category is empty
            return []
        nxt = _vcall(
            enum_mk,
            _ENUMMONIKER_NEXT,
            wintypes.ULONG,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.ULONG),
        )
        index = 0
        while True:
            mk = ctypes.c_void_p()
            fetched = wintypes.ULONG(0)
            if nxt(1, ctypes.byref(mk), ctypes.byref(fetched)) != 0 or fetched.value != 1:
                break
            bag = ctypes.c_void_p()
            try:
                bind = _vcall(
                    mk,
                    _MONIKER_BIND_TO_STORAGE,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.POINTER(GUID),
                    ctypes.POINTER(ctypes.c_void_p),
                )
                if bind(None, None, ctypes.byref(IID_IPropertyBag), ctypes.byref(bag)) < 0:
                    continue
                read = _vcall(bag, _PROPBAG_READ, wintypes.LPCWSTR, ctypes.POINTER(VARIANT), ctypes.c_void_p)

                def prop(name: str, read=read) -> str:
                    var = VARIANT()
                    if read(name, ctypes.byref(var), None) < 0:
                        return ""
                    s = ctypes.wstring_at(var.ptr) if var.vt == VT_BSTR and var.ptr else ""
                    oleaut32.VariantClear(ctypes.byref(var))
                    return s

                out.append(CameraDevice("dshow", index, prop("FriendlyName"), prop("DevicePath")))
            finally:
                _release(bag)
                _release(mk)
                index += 1
    finally:
        _release(enum_mk)
        _release(devenum)
    return out


def _in_mta(fn):
    result: list = []
    error: list[BaseException] = []

    def run() -> None:
        ole32 = _ole32()
        hr = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        try:
            result.append(fn())
        except BaseException as e:  # surfaced to the caller below
            error.append(e)
        finally:
            if hr >= 0:
                ole32.CoUninitialize()

    t = threading.Thread(target=run, name="camera-enum", daemon=True)
    t.start()
    t.join(timeout=15)
    if error:
        raise error[0]
    return result[0] if result else []


def list_cameras(backend: str) -> list[CameraDevice]:
    """Devices for backend "msmf" or "dshow", in OpenCV index order. Empty list on failure."""
    fn = {"msmf": _enum_msmf, "dshow": _enum_dshow}[backend]
    return _in_mta(fn)


def device_key(path: str) -> str:
    """Backend-independent device identity.

    MSMF and DSHOW report the same camera with symbolic links that differ only in the device-interface
    class GUID segment (e.g. "#{e5323777-…}" vs "#{65e8773d-…}"), so that segment is dropped.
    """
    return re.sub(r"#\{[0-9a-f-]{36}\}", "", path.lower())


@dataclass(frozen=True)
class CameraChoice:
    """Which camera the user asked for; empty = automatic."""

    path: str = ""
    name: str = ""
    index: int | None = None


def candidate_indices(devices: list[CameraDevice], choice: CameraChoice) -> tuple[list[int], bool]:
    """(OpenCV indices to try, in order; whether they come from an explicit choice).

    Explicit choice by device path, then name, then raw index. If the chosen device isn't present, fall
    back to automatic order: devices whose name doesn't suggest IR first. Frame content is checked after
    opening in any case.
    """
    if choice.path:
        key = device_key(choice.path)
        hits = [d.index for d in devices if d.path and device_key(d.path) == key]
        if hits:
            return hits, True
    if choice.name:
        hits = [d.index for d in devices if d.name.casefold() == choice.name.casefold()]
        if hits:
            return hits, True
    if choice.index is not None and not choice.path and not choice.name:
        return [choice.index], True
    auto = [d.index for d in devices if not d.looks_ir] + [d.index for d in devices if d.looks_ir]
    return (auto or [0]), False
