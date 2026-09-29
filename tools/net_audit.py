"""Privacy audit: run MediaPipe Face Landmarker and record any TCP connection this process opens.

Dev-time tool. Feeds synthetic frames (no camera, nothing written to disk) through the same
landmarker configuration LookFocus uses, while a background thread polls the Windows TCP table
(IPv4 + IPv6) for connections owned by this PID every 10 ms. Covers task creation, inference and
close(), because some telemetry clients upload on shutdown.

Usage:  .venv\\Scripts\\python.exe tools\\net_audit.py [--seconds 30] [--tail 10]
Exit code 0 = no connections observed, 1 = connections observed.
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

AF_INET, AF_INET6 = 2, 23
TCP_TABLE_OWNER_PID_ALL = 5
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


def tcp_connections(pid: int) -> set[tuple[str, str, str]]:
    """Return {(local, remote, state)} for TCP rows owned by pid."""
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
                if owner == pid:
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
                if owner == pid:
                    loc = f"[{ipaddress.IPv6Address(la)}]:{_port(lp)}"
                    rem = f"[{ipaddress.IPv6Address(ra)}]:{_port(rp)}"
                    out.add((loc, rem, TCP_STATES.get(st, str(st))))
    return out


class Poller(threading.Thread):
    def __init__(self, pid: int, interval: float = 0.01) -> None:
        super().__init__(daemon=True)
        self.pid, self.interval = pid, interval
        self.seen: dict[tuple[str, str, str], tuple[float, str]] = {}
        self.phase = "startup"
        self.polls = 0
        self._halt = threading.Event()
        self.t0 = time.perf_counter()

    def run(self) -> None:
        while not self._halt.is_set():
            for conn in tcp_connections(self.pid):
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
    args = ap.parse_args()

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


if __name__ == "__main__":
    sys.exit(main())
