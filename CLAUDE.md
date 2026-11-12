# LookFocus: working notes for Claude

Tray app for Windows and (branch `linux`) KDE Plasma on Wayland: webcam head pose → which monitor (or zone)
you face → move keyboard focus to the last-used window there. Spec milestones M0–M6; decisions and evidence
live in `DECISIONS.md` (Linux port: D-050 to D-055).

This folder is shared between Windows and Fedora (dual boot): `.venv/` is the Windows venv, `.venv-linux/`
the Linux one. Never touch the other OS's venv.

## Hard rules
- **Never use WSL** to run or test anything (it is sandboxed: no camera, no Win32). Use PowerShell (or the
  Bash tool for plain file work) with the native venv interpreter `.venv\Scripts\python.exe`. On Fedora
  (native Linux, not WSL) use `.venv-linux/bin/python`.
- **Privacy invariants (non-negotiable):** no frames written to disk or logged, no network at runtime, no
  keystroke capture (no global hooks; input activity only via GetLastInputInfo / GetCursorPos /
  GetAsyncKeyState on mouse buttons; on Linux only Wayland idle-notify and KWin's pointer position, never
  /dev/input). Linux UI imports QtCore/QtGui/QtWidgets only, never QtNetwork (a test checks).
- **mediapipe stays pinned at 0.10.33.** 0.10.35+ ship a telemetry uploader (D-003). After any dependency
  change run `pytest` (includes `tests/test_privacy.py`) and `tools\net_audit.py`.
- Pure-logic modules (`classifier`, `decision`, and anything under test with scripted poses) must not import
  Win32/ctypes.windll, cv2 or mediapipe. Inject clocks; don't call `time.monotonic()` directly in them.
- `OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0` (and `OPENBLAS_NUM_THREADS=1`, D-041) are set in `lookfocus/__init__.py`. Scripts must
  `import lookfocus` before `cv2`/`mediapipe`, with `# isort: split` after it so ruff can't reorder it.
- Win32 via ctypes only (no pywin32, D-006). Always set `argtypes`/`restype`.
- Don't trust Win32 return values for focus changes; verify with `GetForegroundWindow` (Linux: KWin's
  active window via the bridge).
- Platform code goes through `backend.py` (lazy imports); Linux counterparts live in `src/lookfocus/linux/`
  with the same public functions as the Win32 module they replace.
- Scope is fixed by the spec; no extra features. Record every non-obvious choice in `DECISIONS.md`.
- Commit at each milestone.

## Commands (PowerShell, from repo root)
```powershell
# one-time setup
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .

# every milestone
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .

# app (M2+)
.venv\Scripts\lookfocus.exe calibrate --zones 2     # or: python -m lookfocus calibrate
.venv\Scripts\lookfocus.exe watch
.venv\Scripts\lookfocus.exe camera-sharing
.venv\Scripts\lookfocus.exe run [--dry-run] [--stats]   # the engine in a console
.venv\Scripts\pythonw.exe -m lookfocus [tray] [--elevated]  # the tray app (what autostart runs)

# tools
.venv\Scripts\python.exe tools\cam_probe.py [--compare-set] [--backends msmf,dshow]
.venv\Scripts\python.exe tools\net_audit.py [--seconds 60 --tail 20]   # or --pid N: watch a running copy
.venv\Scripts\python.exe tools\cam_share_test.py        # can a 2nd app use the camera meanwhile?
.venv\Scripts\python.exe tools\debug_pose.py [--guided]  # live pose viewer; --seconds N auto-quits
.venv\Scripts\python.exe tools\focus_test.py --zones 2 --windows QUERY QUERY  # focus ladder test
.venv\Scripts\python.exe -m lookfocus.camera_activity  # live per-process camera use

# Linux (Fedora / KDE Plasma 6 Wayland; bash from repo root)
#   uv venv --python 3.12 .venv-linux
#   uv pip install --python .venv-linux/bin/python -r requirements-linux.lock
#   uv pip install --python .venv-linux/bin/python --no-deps -e .
#   .venv-linux/bin/python -m pytest; .venv-linux/bin/python -m ruff check .; ... ruff format --check .
#   .venv-linux/bin/python -m lookfocus [calibrate --zones 2 | watch | run --dry-run --stats]  (no arg = tray)
#   .venv-linux/bin/python tools/net_audit.py | tools/debug_pose.py [--no-video]
#   requirements-linux.lock is regenerated with: uv pip freeze --python .venv-linux/bin/python --exclude-editable

# release build (the main way people get LookFocus, D-047): dist\LookFocus\ + dist\LookFocus-<v>-win64.zip, unsigned
.venv\Scripts\python.exe -m pip install -r requirements-build.lock
.venv\Scripts\python.exe tools\build_exe.py
# publish (GitLab, D-048/D-049): upload zip + .sha256 to the generic package registry (glab api --method PUT
#   projects/87333566/packages/generic/lookfocus/<v>/<file> --input <file>), tag vX.Y.Z, then glab release create
#   with --assets-links (link_type package, direct_asset_path /LookFocus-win64.zip). Confirm with the user first.
```

## Layout
- `src/lookfocus/`: package. Pure (no Win32/cv2, unit-tested): pose, displays, calibration, classifier,
  camera_usage (policy), targeting (window choice, activation ladder, stage learner), decision,
  inputactivity (tracker), config.
  Windows: monitors, overlay, camera_enum, camera_sharing, camera_activity, capture, calibrator,
  windows_focus, daemon (engine threads), sysevents, autostart, debugview, tray. CLI: main (`python -m lookfocus`).
  `backend.py` picks the platform modules. Linux (`linux/`): kwin (KWin script bridge over DBus), windowlist
  (pure), focus, monitors, wayland (idle-notify + DPMS, pure-Python wire protocol), input, sysevents, camera
  (PipeWire via gst-launch, V4L2), qt, overlay, viewwindow, tray (Qt icon rendering TrayApp's pystray menu),
  autostart (XDG).
- `tools/`: dev/debug scripts (cam_probe, cam_share_test, net_audit, debug_pose, focus_test, build_exe)
- `packaging/`: PyInstaller spec + shared entry point (dispatches `--probe-child` in the frozen exe)
- `README.md` is the friendly landing page (plain language, no internals); details go in `docs/GUIDE.md`.
  `docs/media/` holds the README video/GIF and tray-icon PNGs (exported from `tray.make_icon`).
  `brag-output/` (gitignored) is the `/brag` Hyperframes project that renders the video.
- `tests/`: pytest; hardware-free except for Windows-only checks that skip elsewhere
- `models/face_landmarker.task`: committed, sha256-checked (see `models/SOURCE.md`)
- User data: `%APPDATA%\LookFocus\` (config.json, calibration.json, logs); Linux: `~/.config/lookfocus/`

## Conventions
- Python 3.12, ruff (line length 110), type hints, dataclasses for plain records.
- Comments explain *why* (Windows quirks, measured behaviour), not what.
