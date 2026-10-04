# PyInstaller spec: one-folder build, two executables sharing one _internal folder.
#   LookFocus.exe      no console: the tray app (what plain launch and autostart run)
#   lookfocus-cli.exe  console: calibrate / watch / run / camera-sharing
# Build with: .venv\Scripts\python.exe tools\build_exe.py   (unsigned; see README "Optional: standalone build")
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = Path(SPECPATH).parent  # noqa: F821 (defined by PyInstaller)
ICON = ROOT / "build" / "lookfocus.ico"  # drawn by tools/build_exe.py (no image assets in the repo)

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
)
# opencv's FFmpeg plugin only serves video files and network streams (and links Winsock); LookFocus reads
# the camera through MSMF/DSHOW, which live in cv2.pyd. Leaving it out removes 29 MB and a network-capable
# library from the build.
a.binaries = [b for b in a.binaries if "opencv_videoio_ffmpeg" not in b[0].lower()]

pyz = PYZ(a.pure)  # noqa: F821
common = dict(exclude_binaries=True, upx=False, icon=str(ICON) if ICON.exists() else None)
gui = EXE(pyz, a.scripts, [], name="LookFocus", console=False, **common)  # noqa: F821
cli = EXE(pyz, a.scripts, [], name="lookfocus-cli", console=True, **common)  # noqa: F821
COLLECT(gui, cli, a.binaries, a.datas, name="LookFocus", upx=False)  # noqa: F821
