"""Privacy audit: run MediaPipe Face Landmarker and record any TCP connection this process opens.

Dev-time tool. Feeds synthetic frames (no camera, nothing written to disk) through the same
landmarker configuration LookFocus uses, while a background thread polls the Windows TCP table
(IPv4 + IPv6) for connections owned by this PID every 10 ms. Covers task creation, inference and
close(), because some telemetry clients upload on shutdown.

Usage:  .venv\\Scripts\\python.exe tools\\net_audit.py [--seconds 30] [--tail 10]
        .venv\\Scripts\\python.exe tools\\net_audit.py --pid PID [--seconds 60]
            watch an already running LookFocus (e.g. the frozen LookFocus.exe) and its child processes
            (the camera-sharing probe) for TCP connections and bound UDP sockets instead.
Exit code 0 = nothing observed, 1 = connections observed.
"""

from __future__ import annotations

import argparse
import ctypes
import ipaddress
import os
import struct
import sys
import threading
import time
from ctypes import wintypes

from lookfocus.privacy import MODEL_PATH as MODEL

_iphlpapi = ctypes.WinDLL("iphlpapi")
_GetExtendedTcpTable = _iphlpapi.GetExtendedTcpTable
_GetExtendedTcpTable.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.BOOL,
    wintypes.ULONG,
    ctypes.c_int,
    wintypes.ULONG,
]
_GetExtendedTcpTable.restype = wintypes.DWORD
_GetExtendedUdpTable = _iphlpapi.GetExtendedUdpTable
_GetExtendedUdpTable.argtypes = _GetExtendedTcpTable.argtypes
_GetExtendedUdpTable.restype = wintypes.DWORD


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32FirstW.restype = wintypes.BOOL
_k32.Process32NextW.argtypes = _k32.Process32FirstW.argtypes
_k32.Process32NextW.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]

AF_INET, AF_INET6 = 2, 23
TCP_TABLE_OWNER_PID_ALL = 5
UDP_TABLE_OWNER_PID = 1
TH32CS_SNAPPROCESS = 0x2
ERROR_INSUFFICIENT_BUFFER = 122
TCP_STATES = {
    1: "CLOSED",
    2: "LISTEN",
    3: "SYN_SENT",
    4: "SYN_RCVD",
    5: "ESTABLISHED",
    6: "FIN_WAIT1",
    7: "FIN_WAIT2",
    8: "CLOSE_WAIT",
    9: "CLOSING",
    10: "LAST_ACK",
    11: "TIME_WAIT",
    12: "DELETE_TCB",
}


def _port(dw: int) -> int:
    return ((dw & 0xFF) << 8) | ((dw >> 8) & 0xFF)


def descendants(root: int) -> set[int]:
    """root plus every still-running process it started, directly or not."""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return {root}
    parent: dict[int, int] = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = _k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            parent[e.th32ProcessID] = e.th32ParentProcessID
            ok = _k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    found, grew = {root}, True
    while grew:
        new = {p for p, pp in parent.items() if pp in found and p not in found and p != pp}
        found |= new
        grew = bool(new)
    return found


def udp_endpoints(pids: set[int]) -> set[tuple[str, str, str]]:
    """Return {(local, "-", "UDP pid=N")} for UDP sockets bound by any of pids (QUIC would show here)."""
    out: set[tuple[str, str, str]] = set()
    for family, row in ((AF_INET, 12), (AF_INET6, 28)):
        size = wintypes.DWORD(0)
        _GetExtendedUdpTable(None, ctypes.byref(size), False, family, UDP_TABLE_OWNER_PID, 0)
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wintypes.DWORD(len(buf))
        if _GetExtendedUdpTable(buf, ctypes.byref(size), False, family, UDP_TABLE_OWNER_PID, 0) != 0:
            continue
        raw = buf.raw
        (n,) = struct.unpack_from("<I", raw, 0)
        for i in range(n):
            off = 4 + i * row
            if family == AF_INET:
                la, lp, owner = struct.unpack_from("<3I", raw, off)
                loc = f"{ipaddress.IPv4Address(struct.pack('<I', la))}:{_port(lp)}"
            else:
                _, lp, owner = struct.unpack_from("<3I", raw, off + 16)
                loc = f"[{ipaddress.IPv6Address(raw[off : off + 16])}]:{_port(lp)}"
            if owner in pids:
                out.add((loc, "-", f"UDP pid={owner}"))
    return out


def tcp_connections(pid: int | set[int]) -> set[tuple[str, str, str]]:
    """Return {(local, remote, state)} for TCP rows owned by pid (or any pid in a set)."""
    pids = {pid} if isinstance(pid, int) else pid
    out: set[tuple[str, str, str]] = set()
    for family in (AF_INET, AF_INET6):
        size = wintypes.DWORD(0)
        _GetExtendedTcpTable(None, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_ALL, 0)
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wintypes.DWORD(len(buf))
        rc = _GetExtendedTcpTable(buf, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_ALL, 0)
        if rc != 0:
            continue
        raw = buf.raw
        (n,) = struct.unpack_from("<I", raw, 0)
        if family == AF_INET:
            for i in range(n):
                st, la, lp, ra, rp, owner = struct.unpack_from("<6I", raw, 4 + i * 24)
                if owner in pids:
                    loc = f"{ipaddress.IPv4Address(struct.pack('<I', la))}:{_port(lp)}"
                    rem = f"{ipaddress.IPv4Address(struct.pack('<I', ra))}:{_port(rp)}"
                    out.add((loc, rem, TCP_STATES.get(st, str(st))))
        else:
            for i in range(n):
                off = 4 + i * 56
                la = raw[off : off + 16]
                _, lp = struct.unpack_from("<II", raw, off + 16)
                ra = raw[off + 24 : off + 40]
                _, rp, st, owner = struct.unpack_from("<4I", raw, off + 40)
                if owner in pids:
                    loc = f"[{ipaddress.IPv6Address(la)}]:{_port(lp)}"
                    rem = f"[{ipaddress.IPv6Address(ra)}]:{_port(rp)}"
                    out.add((loc, rem, TCP_STATES.get(st, str(st))))
    return out


class Poller(threading.Thread):
    def __init__(self, pid: int, interval: float = 0.01, follow: bool = False) -> None:
        super().__init__(daemon=True)
        self.pid, self.interval, self.follow = pid, interval, follow
        self.pids, self.all_pids = {pid}, {pid}
        self.seen: dict[tuple[str, str, str], tuple[float, str]] = {}
        self.phase = "startup"
        self.polls = 0
        self._halt = threading.Event()
        self.t0 = time.perf_counter()

    def run(self) -> None:
        while not self._halt.is_set():
            if self.follow and self.polls % 20 == 0:  # child processes, refreshed every ~0.2 s
                self.pids = descendants(self.pid)
                self.all_pids |= self.pids
            conns = tcp_connections(self.pids) | (udp_endpoints(self.pids) if self.follow else set())
            for conn in conns:
                self.seen.setdefault(conn, (time.perf_counter() - self.t0, self.phase))
            self.polls += 1
            self._halt.wait(self.interval)

    def stop(self) -> None:
        self._halt.set()
        self.join()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=30.0, help="inference duration")
    ap.add_argument("--tail", type=float, default=10.0, help="keep polling this long after close()")
    ap.add_argument("--pid", type=int, help="watch this running process and its children instead")
    args = ap.parse_args()
    if args.pid:
        return watch(args.pid, args.seconds)

    if not MODEL.exists():
        print(f"model not found: {MODEL}")
        return 2

    poller = Poller(os.getpid())
    poller.start()

    poller.phase = "import"
    import mediapipe as mp
    import numpy as np

    poller.phase = "create"
    opts = mp.tasks.vision.FaceLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(MODEL)),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_faces=1,
        output_facial_transformation_matrixes=True,
    )
    landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(opts)

    poller.phase = "inference"
    rng = np.random.default_rng(0)
    frames = 0
    t_end = time.perf_counter() + args.seconds
    ts = 0
    while time.perf_counter() < t_end:
        img = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
        landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=img), ts)
        ts += 66
        frames += 1
        time.sleep(1 / 15)

    poller.phase = "close"
    landmarker.close()
    del landmarker

    poller.phase = "tail"
    time.sleep(args.tail)
    poller.stop()

    elapsed = time.perf_counter() - poller.t0
    print(f"mediapipe {mp.__version__}: {frames} frames, {poller.polls} TCP-table polls over {elapsed:.1f}s")
    if not poller.seen:
        print("RESULT: no TCP connections owned by this process were observed.")
        return 0
    print(f"RESULT: {len(poller.seen)} TCP connection(s) observed:")
    for (loc, rem, st), (t, phase) in sorted(poller.seen.items(), key=lambda kv: kv[1][0]):
        print(f"  t={t:6.2f}s phase={phase:<9} {loc} -> {rem} {st}")
    return 1


def watch(pid: int, seconds: float) -> int:
    poller = Poller(pid, follow=True)
    poller.phase = "watch"
    poller.start()
    time.sleep(seconds)
    poller.stop()
    children = sorted(poller.all_pids - {pid}) or "none"
    print(f"watched pid {pid} (children seen: {children}), {poller.polls} table polls over {seconds:.0f}s")
    if not poller.seen:
        print("RESULT: no TCP connections or UDP sockets owned by these processes were observed.")
        return 0
    print(f"RESULT: {len(poller.seen)} endpoint(s) observed:")
    for (loc, rem, st), (t, _phase) in sorted(poller.seen.items(), key=lambda kv: kv[1][0]):
        print(f"  t={t:6.2f}s {loc} -> {rem} {st}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
