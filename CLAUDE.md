# LookFocus — working notes for Claude

Windows tray app: webcam head pose → which monitor (or zone) you face → move keyboard focus to the
last-used window there. Spec milestones M0–M6; decisions and evidence live in `DECISIONS.md`.

## Hard rules
- **Never use WSL** to run or test anything (it is sandboxed: no camera, no Win32). Use PowerShell (or the
  Bash tool for plain file work) with the native venv interpreter `.venv\Scripts\python.exe`.
- **Privacy invariants (non-negotiable):** no frames written to disk or logged, no network at runtime, no
  keystroke capture (no global hooks; input activity only via GetLastInputInfo / GetCursorPos /
  GetAsyncKeyState on mouse buttons).
- **mediapipe stays pinned at 0.10.33.** 0.10.35+ ship a telemetry uploader (D-003). After any dependency
  change run `pytest` (includes `tests/test_privacy.py`) and `tools\net_audit.py`.
- Pure-logic modules (`classifier`, `decision`, and anything under test with scripted poses) must not import
  Win32/ctypes.windll, cv2 or mediapipe. Inject clocks; don't call `time.monotonic()` directly in them.
- `OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0` is set in `lookfocus/__init__.py`. Scripts must
  `import lookfocus` before `cv2`/`mediapipe`, with `# isort: split` after it so ruff can't reorder it.
- Win32 via ctypes only (no pywin32, D-006). Always set `argtypes`/`restype`.
- Don't trust Win32 return values for focus changes; verify with `GetForegroundWindow`.
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

# tools
.venv\Scripts\python.exe tools\cam_probe.py [--compare-set] [--backends msmf,dshow]
.venv\Scripts\python.exe tools\net_audit.py [--seconds 60 --tail 20]
.venv\Scripts\python.exe tools\cam_share_test.py        # can a 2nd app use the camera meanwhile?
.venv\Scripts\python.exe tools\debug_pose.py [--guided]  # live pose viewer; --seconds N auto-quits
.venv\Scripts\python.exe tools\focus_test.py --zones 2 --windows QUERY QUERY  # focus ladder test
.venv\Scripts\python.exe -m lookfocus.camera_activity  # live per-process camera use
```

## Layout
- `src/lookfocus/` — package. Pure (no Win32/cv2, unit-tested): pose, displays, calibration, classifier,
  camera_usage (policy), targeting (window choice, activation ladder, stage learner), decision,
  inputactivity (tracker).
  Windows: monitors, overlay, camera_enum, camera_sharing, camera_activity, capture, calibrator,
  windows_focus, daemon (engine threads). CLI: main (`python -m lookfocus`).
- `tools/` — dev/debug scripts (cam_probe, cam_share_test, net_audit, debug_pose, focus_test)
- `tests/` — pytest; hardware-free except for Windows-only checks that skip elsewhere
- `models/face_landmarker.task` — committed, sha256-checked (see `models/SOURCE.md`)
- User data: `%APPDATA%\LookFocus\` (config.json, calibration.json, logs)

## Conventions
- Python 3.12, ruff (line length 110), type hints, dataclasses for plain records.
- Comments explain *why* (Windows quirks, measured behaviour), not what.
