"""Checks backing the "no network at runtime" invariant.

mediapipe 0.10.35+ wheels bundle a Clearcut telemetry uploader in libmediapipe.dll that POSTs usage
metrics to play.googleapis.com (see DECISIONS.md, D-003). We pin a clean version; this module lets
tests (and the app at startup) verify that the mediapipe native library actually loaded links no
network API, so an accidental upgrade fails loudly instead of silently phoning home.

Windows (PE): networking lives in separate DLLs (ws2_32, winhttp, ...), so the import table names them.
Linux (ELF): every library links libc, which has the socket calls, so the check reads the library's
*undefined dynamic symbols* instead: what it calls into from elsewhere (socket, connect, getaddrinfo, ...,
plus curl / TLS libraries). The pinned 0.10.33 Linux build calls none of them (measured, D-050).

Pure file parsing; no Win32 calls, so it is testable anywhere.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from lookfocus.paths import MODEL_PATH

NETWORK_DLLS = frozenset(
    {
        "ws2_32.dll",
        "wsock32.dll",
        "mswsock.dll",
        "wininet.dll",
        "winhttp.dll",
        "urlmon.dll",
        "httpapi.dll",
        "dnsapi.dll",
        "webio.dll",
    }
)

# Imported (undefined) symbols that mean a library talks to the network itself.
NETWORK_SYMBOLS = frozenset(
    {
        "socket",
        "connect",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_r",
        "getnameinfo",
        "sendto",
        "sendmsg",
        "res_query",
        "res_nquery",
    }
)
NETWORK_SYMBOL_PREFIXES = ("curl_", "SSL_", "gnutls_", "nghttp2_")
NETWORK_SONAMES = ("libcurl", "libssl", "libgnutls", "libnghttp2", "libnss3")

MODEL_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"


def pe_imported_dlls(data: bytes) -> set[str]:
    """Lower-cased names of DLLs in a PE image's import and delay-import tables."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        raise ValueError("not a PE image")
    coff = pe + 4
    (nsec,) = struct.unpack_from("<H", data, coff + 2)
    (opt_size,) = struct.unpack_from("<H", data, coff + 16)
    opt = coff + 20
    (magic,) = struct.unpack_from("<H", data, opt)
    ddir = opt + (112 if magic == 0x20B else 96)
    sections = []
    for i in range(nsec):
        vsize, va, rsize, rptr = struct.unpack_from("<IIII", data, opt + opt_size + i * 40 + 8)
        sections.append((va, max(vsize, rsize), rptr))

    def offset(rva: int) -> int | None:
        for va, size, ptr in sections:
            if va <= rva < va + size:
                return rva - va + ptr
        return None

    names: set[str] = set()
    # (data-directory index, descriptor size, offset of the DLL-name RVA within the descriptor)
    for dir_index, desc_size, name_field in ((1, 20, 12), (13, 32, 4)):
        (rva,) = struct.unpack_from("<I", data, ddir + dir_index * 8)
        off = offset(rva) if rva else None
        while off is not None:
            desc = data[off : off + desc_size]
            (name_rva,) = struct.unpack_from("<I", desc, name_field)
            if desc == b"\0" * desc_size or not name_rva:
                break
            name_off = offset(name_rva)
            if name_off is not None:
                names.add(data[name_off : data.index(b"\0", name_off)].decode("ascii", "replace").lower())
            off += desc_size
    return names


def elf_imports(data: bytes) -> tuple[set[str], set[str]]:
    """(undefined dynamic symbol names, DT_NEEDED sonames) of a 64-bit little-endian ELF image."""
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ValueError("not a 64-bit little-endian ELF image")
    (shoff,) = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]

    def cstr(tab: int, off: int) -> str:
        start = sections[tab][4] + off
        return data[start : data.index(b"\0", start)].decode("ascii", "replace")

    symbols: set[str] = set()
    needed: set[str] = set()
    for _name, sh_type, _flags, _addr, offset, size, link, _info, _align, entsize in sections:
        if sh_type == 11:  # SHT_DYNSYM: Elf64_Sym is (name u32, info u8, other u8, shndx u16, value, size)
            for k in range(size // (entsize or 24)):
                st_name, _i, _o, shndx = struct.unpack_from("<IBBH", data, offset + k * (entsize or 24))
                if shndx == 0 and st_name:
                    symbols.add(cstr(link, st_name))
        elif sh_type == 6:  # SHT_DYNAMIC: (tag i64, value u64); DT_NEEDED = 1
            for k in range(size // (entsize or 16)):
                tag, val = struct.unpack_from("<qQ", data, offset + k * (entsize or 16))
                if tag == 1:
                    needed.add(cstr(link, val))
                elif tag == 0:
                    break
    return symbols, needed


def network_imports(data: bytes) -> set[str]:
    """Network APIs a native binary (PE or ELF) links; empty set means clean."""
    if data[:4] == b"\x7fELF":
        symbols, needed = elf_imports(data)
        hits = {s for s in symbols if s in NETWORK_SYMBOLS or s.startswith(NETWORK_SYMBOL_PREFIXES)}
        return hits | {n for n in needed if n.startswith(NETWORK_SONAMES)}
    return pe_imported_dlls(data) & NETWORK_DLLS


def mediapipe_native_libs() -> list[Path]:
    """Native binaries shipped inside the installed mediapipe package (found without importing it)."""
    import importlib.util

    spec = importlib.util.find_spec("mediapipe")
    if spec is None or not spec.submodule_search_locations:
        return []
    pkg = Path(next(iter(spec.submodule_search_locations)))
    return sorted(
        p for p in pkg.rglob("*") if p.suffix.lower() in (".dll", ".pyd", ".so") or ".so." in p.name
    )


def network_linked_libs(paths: list[Path]) -> dict[Path, set[str]]:
    """Map of binary -> network DLLs (Windows) or symbols / libraries (Linux) it links. Empty = clean."""
    bad = {}
    for p in paths:
        hits = network_imports(p.read_bytes())
        if hits:
            bad[p] = hits
    return bad


def model_sha256(path: Path = MODEL_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PrivacyError(RuntimeError):
    pass


def verify_runtime(model_path: Path = MODEL_PATH) -> None:
    """Refuse to run vision with a network-capable mediapipe build or an unexpected model file.

    Called before mediapipe is imported. Costs ~50 ms (reads the ~28 MB native library once).
    """
    libs = mediapipe_native_libs()
    if not libs:
        raise PrivacyError("mediapipe native library not found")
    bad = network_linked_libs(libs)
    if bad:
        names = ", ".join(f"{p.name}: {sorted(d)}" for p, d in bad.items())
        raise PrivacyError(
            f"installed mediapipe links network APIs ({names}); this build can send telemetry. "
            "Reinstall the pinned version: pip install -r requirements.lock (see DECISIONS.md D-003)."
        )
    if not model_path.exists():
        raise PrivacyError(f"model file missing: {model_path}")
    if model_path == MODEL_PATH and model_sha256(model_path) != MODEL_SHA256:
        raise PrivacyError(f"model file {model_path} does not match the recorded SHA-256 (models/SOURCE.md)")
