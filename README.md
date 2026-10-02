# LookFocus

Look at a monitor and just type: LookFocus uses your webcam to work out which monitor you are facing and moves
keyboard focus to the window you last used on that monitor. Windows 11, runs from the tray, fully offline.

> Status: **M5 (tray app) done, awaiting hardware check.** Not usable yet. Full setup, tuning
> and limitations docs land in M6.

## Privacy
- Camera frames are processed in memory only: never saved, never logged, never sent anywhere.
- No network access at runtime. mediapipe is pinned to 0.10.33 because newer releases send usage telemetry
  to Google (see `DECISIONS.md` D-003); a test and `tools/net_audit.py` verify this.
- No keyboard hooks and no keystroke capture: typing is inferred only from "some input happened recently"
  (`GetLastInputInfo`) versus mouse movement.

## Dev setup (Windows, native Python 3.12)
```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
.venv\Scripts\python.exe tools\cam_probe.py
```

## Running (until the full guide in M6)
```powershell
.venv\Scripts\lookfocus.exe calibrate --zones 2      # once (zones only if you have a single monitor)
.venv\Scripts\pythonw.exe -m lookfocus               # the tray app (no console window)
```
Right-click the tray icon for Pause/Resume, Recalibrate, Choose camera, Camera sharing, Open config,
Debug view, Start with Windows and Quit. **Ctrl+Alt+Shift+G** pauses and resumes. LookFocus pauses (and
releases the camera) while the PC is locked, asleep or the screen is off. Settings live in
`%APPDATA%\LookFocus\config.json` (changes apply within a few seconds); logs in
`%APPDATA%\LookFocus\logs\` (text only; window titles appear truncated).
