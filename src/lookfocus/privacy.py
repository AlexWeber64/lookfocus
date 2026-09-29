"""Checks backing the "no network at runtime" invariant.

mediapipe 0.10.35+ wheels bundle a Clearcut telemetry uploader in libmediapipe.dll that POSTs usage
metrics to play.googleapis.com (see DECISIONS.md, D-003). We pin a clean version; this module lets
tests (and the app at startup) verify that the mediapipe native library actually loaded links no
network API, so an accidental upgrade fails loudly instead of silently phoning home.

Pure file parsing (PE import table); no Win32 calls, so it is testable anywhere.
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


def mediapipe_native_libs() -> list[Path]:
    """Native binaries shipped inside the installed mediapipe package (found without importing it)."""
    import importlib.util

    spec = importlib.util.find_spec("mediapipe")
    if spec is None or not spec.submodule_search_locations:
        return []
    pkg = Path(next(iter(spec.submodule_search_locations)))
    return sorted(p for p in pkg.rglob("*") if p.suffix.lower() in (".dll", ".pyd"))


def network_linked_libs(paths: list[Path]) -> dict[Path, set[str]]:
    """Map of binary -> network DLLs it links. Empty dict means clean."""
    bad = {}
    for p in paths:
        hits = pe_imported_dlls(p.read_bytes()) & NETWORK_DLLS
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
