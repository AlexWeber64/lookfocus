"""Optional standalone build: PyInstaller one-folder build in dist\\LookFocus, then privacy checks on it.

Usage:  .venv\\Scripts\\python.exe -m pip install -r requirements-build.lock
        .venv\\Scripts\\python.exe tools\\build_exe.py

The result is unsigned; antivirus products sometimes flag PyInstaller executables (see README).
Exit code 0 = built and checks passed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import lookfocus  # noqa: F401

# isort: split
from lookfocus.privacy import MODEL_SHA256, model_sha256, network_linked_libs
from lookfocus.tray import make_icon

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "LookFocus"


def check(dist: Path) -> list[str]:
    problems = []
    for exe in ("LookFocus.exe", "lookfocus-cli.exe"):
        if not (dist / exe).exists():
            problems.append(f"missing {exe}")
    internal = dist / "_internal"
    model = internal / "models" / "face_landmarker.task"
    if not model.exists() or model_sha256(model) != MODEL_SHA256:
        problems.append("model missing or SHA-256 mismatch")
    mp_libs = sorted(p for p in (internal / "mediapipe").rglob("*") if p.suffix.lower() in (".dll", ".pyd"))
    if not mp_libs:
        problems.append("mediapipe native library missing")
    for lib, dlls in network_linked_libs(mp_libs).items():
        problems.append(f"{lib.name} links network APIs {sorted(dlls)} (telemetry build?)")
    if any(internal.rglob("opencv_videoio_ffmpeg*")):
        problems.append("opencv FFmpeg plugin was bundled")
    return problems


def main() -> int:
    icon = ROOT / "build" / "lookfocus.ico"
    icon.parent.mkdir(exist_ok=True)
    make_icon("active", 256).save(icon, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    spec = ROOT / "packaging" / "lookfocus.spec"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN"]
    cmd += ["--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "pyinstaller"), str(spec)]
    print("running:", subprocess.list2cmdline(cmd), flush=True)
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc:
        print(f"PyInstaller failed ({rc})")
        return rc
    problems = check(DIST)
    size = sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file()) / 2**20
    smoke = subprocess.run([str(DIST / "lookfocus-cli.exe"), "--help"], capture_output=True, text=True)
    if smoke.returncode != 0 or "calibrate" not in smoke.stdout:
        problems.append(f"lookfocus-cli.exe --help failed ({smoke.returncode}): {smoke.stderr[-300:]}")
    # The bundle must be able to run the face tracker (privacy checks + model + mediapipe + inference).
    selfcheck = subprocess.run(
        [str(DIST / "lookfocus-cli.exe"), "--self-check"], capture_output=True, text=True, timeout=120
    )
    if selfcheck.returncode != 0 or "self-check OK" not in selfcheck.stdout:
        problems.append(f"--self-check failed ({selfcheck.returncode}): {selfcheck.stderr[-400:]}")
    for p in problems:
        print("PROBLEM:", p)
    print(f"{DIST} ({size:.0f} MB): {'OK' if not problems else 'FAILED'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
