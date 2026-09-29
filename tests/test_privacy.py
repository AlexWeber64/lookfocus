"""Guards for the privacy invariants that can be checked statically."""

import sys

import pytest

from lookfocus import privacy


@pytest.mark.skipif(sys.platform != "win32", reason="mediapipe native lib is a Windows DLL here")
def test_mediapipe_native_libs_link_no_network_api():
    libs = privacy.mediapipe_native_libs()
    assert libs, "mediapipe native library not found; is mediapipe installed?"
    bad = privacy.network_linked_libs(libs)
    assert not bad, (
        f"mediapipe binaries link network APIs: {bad}. mediapipe >= 0.10.35 bundles a telemetry "
        "uploader; keep the pin at 0.10.33 (DECISIONS.md D-003) and re-run tools/net_audit.py."
    )


def test_pe_parser_detects_network_import():
    # Real system DLL known to import networking: urlmon links wininet.
    import os
    from pathlib import Path

    urlmon = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "urlmon.dll"
    if not urlmon.exists():
        pytest.skip("urlmon.dll not available")
    assert "wininet.dll" in privacy.pe_imported_dlls(urlmon.read_bytes())


def test_model_file_matches_recorded_hash():
    assert privacy.MODEL_PATH.exists(), "models/face_landmarker.task missing (see models/SOURCE.md)"
    assert privacy.model_sha256() == privacy.MODEL_SHA256
