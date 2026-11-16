"""Linux socket tables for net_audit.py: which TCP connections / UDP sockets belong to given processes.

/proc/net/{tcp,tcp6,udp,udp6} list every socket in the network namespace with its inode; a process's
sockets are the `socket:[inode]` links in /proc/<pid>/fd. Matching the two gives per-process endpoints,
the Linux counterpart of GetExtendedTcpTable's owner PID column.
"""

from __future__ import annotations

import ipaddress
import os
import struct
from pathlib import Path

TCP_STATES = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
}


def descendants(root: int) -> set[int]:
    """root plus every still-running process it started, directly or not."""
    parent: dict[int, int] = {}
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                fields = (d / "stat").read_text().rsplit(")", 1)[1].split()
                parent[int(d.name)] = int(fields[1])
            except (OSError, IndexError, ValueError):
                continue
    found, grew = {root}, True
    while grew:
        new = {p for p, pp in parent.items() if pp in found and p not in found}
        found |= new
        grew = bool(new)
    return found


def _socket_inodes(pids: set[int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for pid in pids:
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                out[target[8:-1]] = pid
    return out


def _addr(hexaddr: str) -> str:
    ip, port = hexaddr.split(":")
    raw = bytes.fromhex(ip)
    if len(raw) == 4:
        host = str(ipaddress.IPv4Address(struct.pack("<I", int.from_bytes(raw, "big"))))
    else:
        words = struct.unpack(">4I", raw)
        host = f"[{ipaddress.IPv6Address(b''.join(struct.pack('<I', w) for w in words))}]"
    return f"{host}:{int(port, 16)}"


def _table(kind: str, pids: set[int]) -> list[tuple[str, str, str, int]]:
    inodes = _socket_inodes(pids)
    rows = []
    for name in (kind, kind + "6"):
        try:
            lines = Path(f"/proc/net/{name}").read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) > 9 and f[9] in inodes:
                rows.append((_addr(f[1]), _addr(f[2]), f[3], inodes[f[9]]))
    return rows


def tcp_connections(pid: int | set[int]) -> set[tuple[str, str, str]]:
    pids = {pid} if isinstance(pid, int) else pid
    return {(loc, rem, TCP_STATES.get(st, st)) for loc, rem, st, _ in _table("tcp", pids)}


def udp_endpoints(pids: set[int]) -> set[tuple[str, str, str]]:
    return {(loc, "-", f"UDP pid={owner}") for loc, _rem, _st, owner in _table("udp", pids)}
