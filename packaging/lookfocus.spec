# PyInstaller spec: one-folder build, two executables sharing one _internal folder.
#   LookFocus.exe      no console: the tray app (what plain launch and autostart run)
#   lookfocus-cli.exe  console: calibrate / watch / run / camera-sharing
# Build with: .venv\Scripts\python.exe tools\build_exe.py   (unsigned; see docs/GUIDE.md "Standalone build")
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = Path(SPECPATH).parent  # noqa: F821 (defined by PyInstaller)
BUILD = ROOT / "build"
ICON = BUILD / "lookfocus.ico"  # drawn by tools/build_exe.py (no image assets in the repo)

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    # mediapipe loads tasks/c/libmediapipe.dll with ctypes via importlib.resources, so PyInstaller can't see
    # it; it must keep its package-relative folder (privacy.verify_runtime checks it there too).
    binaries=collect_dynamic_libs("mediapipe"),
    datas=[
        (str(ROOT / "models" / "face_landmarker.task"), "models"),
        (str(ROOT / "models" / "SOURCE.md"), "models"),
    ],
    # Not used by LookFocus. matplotlib must stay: mediapipe's vision package imports its drawing utils,
    # which import matplotlib.pyplot (measured: excluding it broke the face tracker). sounddevice is
    # imported inside a try/except by mediapipe's audio helpers only.
    excludes=["sounddevice", "tkinter", "pytest", "IPython", "jax", "torch", "tensorflow"],
    # pystray is LGPL-3.0: ship it as plain .py files in _internal\pystray (not inside the exe's archive),
    # so users can replace it with a modified version, as the LGPL asks of bundled libraries.
    module_collection_mode={"pystray": "py"},
)
# opencv's FFmpeg plugin only serves video files and network streams (and links Winsock); LookFocus reads
# the camera through MSMF/DSHOW, which live in cv2.pyd. Leaving it out removes 29 MB and a network-capable
# library from the build.
a.binaries = [b for b in a.binaries if "opencv_videoio_ffmpeg" not in b[0].lower()]

# Which top-level packages ended up in the bundle: tools/build_exe.py collects their license files.
tops = {name.split(".")[0] for name, *_ in a.pure}
tops |= {Path(dest).parts[0] for dest, *_ in [*a.binaries, *a.datas]}
(BUILD / "bundled-top-level.txt").write_text("\n".join(sorted(tops)), encoding="utf-8")

pyz = PYZ(a.pure)  # noqa: F821


def exe(name, console, version_file):
    version = BUILD / version_file  # written by tools/build_exe.py (Properties > Details in Explorer)
    return EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        name=name,
        console=console,
        exclude_binaries=True,
        upx=False,
        icon=str(ICON) if ICON.exists() else None,
        version=str(version) if version.exists() else None,
    )


gui = exe("LookFocus", False, "version-gui.txt")
cli = exe("lookfocus-cli", True, "version-cli.txt")
COLLECT(gui, cli, a.binaries, a.datas, name="LookFocus", upx=False)  # noqa: F821
