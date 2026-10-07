"""Standalone build: PyInstaller one-folder build in dist\\LookFocus, checks, and the zip people download.

Usage:  .venv\\Scripts\\python.exe -m pip install -r requirements-build.lock
        .venv\\Scripts\\python.exe tools\\build_exe.py

Output: dist\\LookFocus\\ (LookFocus.exe, lookfocus-cli.exe, _internal\\, README.txt, third-party-licenses\\)
        dist\\LookFocus-<version>-win64.zip (that folder, ready to attach to a release)
The result is unsigned; antivirus products sometimes flag PyInstaller executables (see docs/GUIDE.md).
Exit code 0 = built and checks passed.
"""

from __future__ import annotations

import importlib.metadata as md
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import lookfocus

# isort: split
from lookfocus.privacy import MODEL_SHA256, model_sha256, network_linked_libs
from lookfocus.tray import make_icon

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DIST = ROOT / "dist" / "LookFocus"
VERSION = lookfocus.__version__
ZIP = ROOT / "dist" / f"LookFocus-{VERSION}-win64.zip"

_VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={tup}, prodvers={tup}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', '{description}'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', 'LookFocus'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def write_build_inputs() -> None:
    BUILD.mkdir(exist_ok=True)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)]
    make_icon("active", 256).save(BUILD / "lookfocus.ico", sizes=sizes)
    parts = [int(p) for p in VERSION.split(".")[:3]]
    tup = tuple([*parts, 0][:4])
    for file, name, description in (
        ("version-gui.txt", "LookFocus", "LookFocus"),
        ("version-cli.txt", "lookfocus-cli", "LookFocus command line"),
    ):
        text = _VERSION_INFO.format(tup=tup, version=VERSION, name=name, description=description)
        (BUILD / file).write_text(text, encoding="utf-8")


def _license_name(dist: md.Distribution) -> str:
    meta = dist.metadata
    name = meta.get("License-Expression") or ""
    if not name:
        classifiers = [
            c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License")
        ]
        name = ", ".join(classifiers) or (meta.get("License") or "").splitlines()[0][:60]
    return name or "see license files"


def collect_licenses(dist_dir: Path) -> list[str]:
    """Copy the license files of every bundled distribution (plus Python and PyInstaller's bootloader)."""
    out = dist_dir / "third-party-licenses"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    tops = (BUILD / "bundled-top-level.txt").read_text(encoding="utf-8").split()
    by_top = md.packages_distributions()
    dists = sorted({d for t in tops for d in by_top.get(t, [])} - {"lookfocus"}, key=str.lower)
    rows = []
    for name in dists:
        dist = md.distribution(name)
        files = [
            f
            for f in dist.files or []
            if any(k in f.name.upper() for k in ("LICEN", "COPYING", "NOTICE")) and f.suffix != ".py"
        ]
        target = out / f"{name}-{dist.version}"
        for f in files:
            dest = target / f.name
            if dest.exists():  # e.g. two LICENSE files in different folders
                dest = target / f"{f.parent.name}-{f.name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dist.locate_file(f), dest)
        note = "" if files else "  (no license file shipped; Apache-2.0 text: see mediapipe's LICENSE)"
        rows.append(f"{name} {dist.version}: {_license_name(dist)}{note}")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        (out / "python").mkdir()
        shutil.copyfile(python_license, out / "python" / "LICENSE.txt")
        rows.append(f"Python {sys.version.split()[0]}: PSF-2.0 (python/LICENSE.txt)")
    pyi = md.distribution("pyinstaller")
    for f in pyi.files or []:
        if f.name.upper().startswith("COPYING"):
            (out / "pyinstaller").mkdir(exist_ok=True)
            shutil.copyfile(pyi.locate_file(f), out / "pyinstaller" / f.name)
    rows.append(
        f"PyInstaller {pyi.version} bootloader: GPL-2.0 with the bootloader exception (pyinstaller/), "
        "which allows distributing programs built with it under any license"
    )
    index = [
        "Third-party software bundled with LookFocus",
        "=============================================",
        "",
        *rows,
        "",
        "pystray (LGPL-3.0) is shipped as plain .py files in _internal\\pystray, so it can be replaced",
        "with a modified version. Its source is also at https://github.com/moses-palmer/pystray.",
        "The face model (_internal\\models\\face_landmarker.task) is a MediaPipe model, Apache-2.0; see",
        "_internal\\models\\SOURCE.md.",
        "",
    ]
    (out / "README.txt").write_text("\n".join(index), encoding="utf-8")
    return rows


def check(dist: Path) -> list[str]:
    problems = []
    for name in ("LookFocus.exe", "lookfocus-cli.exe", "README.txt", "LICENSE.txt"):
        if not (dist / name).exists():
            problems.append(f"missing {name}")
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
    if not (internal / "pystray" / "__init__.py").exists():
        problems.append("pystray is not shipped as replaceable .py files (LGPL)")
    if not any((dist / "third-party-licenses").rglob("LICENSE*")):
        problems.append("third-party license files missing")
    return problems


def make_zip(dist: Path, zip_path: Path) -> None:
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(dist.rglob("*")):
            if f.is_file():
                z.write(f, Path("LookFocus") / f.relative_to(dist))


def main() -> int:
    write_build_inputs()
    spec = ROOT / "packaging" / "lookfocus.spec"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN"]
    cmd += ["--distpath", str(ROOT / "dist"), "--workpath", str(BUILD / "pyinstaller"), str(spec)]
    print("running:", subprocess.list2cmdline(cmd), flush=True)
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc:
        print(f"PyInstaller failed ({rc})")
        return rc
    readme = (ROOT / "packaging" / "README.txt").read_text(encoding="utf-8").format(version=VERSION)
    (DIST / "README.txt").write_text(readme, encoding="utf-8")
    shutil.copyfile(ROOT / "LICENSE", DIST / "LICENSE.txt")  # GPL-3.0: the license travels with the binary
    rows = collect_licenses(DIST)
    print(f"third-party licenses: {len(rows)} entries")
    problems = check(DIST)
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
    size = sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file()) / 2**20
    if problems:
        print(f"{DIST} ({size:.0f} MB): FAILED (no zip made)")
        return 1
    make_zip(DIST, ZIP)
    print(f"{DIST} ({size:.0f} MB): OK")
    print(f"{ZIP} ({ZIP.stat().st_size / 2**20:.0f} MB): ready to attach to a release")
    return 0


if __name__ == "__main__":
    sys.exit(main())
