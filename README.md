# LookFocus

Look at a monitor and just type. LookFocus watches your head pose through the webcam, works out which monitor
you're facing, and moves keyboard focus to the window you last used on that monitor. It runs in the tray on
Windows 11 and works fully offline.

It follows your **head**, not your eyes: turn your head toward a screen the way you normally do and keep it
there for a moment, and the window there gets focus. A quick glance does nothing.

> Status: v1 complete (milestones M0–M6). Tested on one machine: a ThinkPad X9 15 with its built-in camera
> and one physical monitor split into zones. Real multi-monitor setups haven't been tested yet; see
> [Known limitations](#known-limitations). `DECISIONS.md` records every design choice and measurement.

## Contents
- [Privacy](#privacy)
- [Requirements](#requirements)
- [Install](#install)
- [First run: calibrate](#first-run-calibrate)
- [Everyday use](#everyday-use)
- [Camera placement and lighting](#camera-placement-and-lighting)
- [Tuning guide](#tuning-guide)
- [Video calls and camera sharing](#video-calls-and-camera-sharing)
- [Administrator windows](#administrator-windows)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Optional: standalone build](#optional-standalone-build)
- [Uninstall](#uninstall)
- [Development](#development)

## Privacy
- **Camera images stay in memory.** They're never saved, logged or sent anywhere. Each frame is reduced to
  three head angles and then dropped.
- **No network.** LookFocus never connects to anything at runtime. mediapipe is pinned to 0.10.33, because
  later releases include a usage-telemetry uploader (`DECISIONS.md` D-003). Two checks guard this. At
  startup the app refuses to run if the mediapipe library links any network API or the face model's
  SHA-256 is wrong. `tools/net_audit.py` watches for real connections.
- **No keylogging.** LookFocus installs no keyboard or mouse hooks and never reads which keys you press. "You
  are typing" is inferred from "some input happened that wasn't the mouse" (`GetLastInputInfo` plus pointer
  position and mouse-button state).
- **Logs** (`%APPDATA%\LookFocus\logs`) are text only: state changes, which display you faced, and the
  window that got focus, with titles cut to 40 characters. They stay on your PC; delete them any time.

## Requirements
- Windows 11, x64. Windows 10 may work but hasn't been tested.
- A colour webcam: built-in or USB, roughly in front of you. A Windows Hello IR camera is detected and
  skipped automatically.
- Two or more monitors, or one monitor split into zones (see below).
- From source: Python 3.12 (x64) from python.org, and about 700 MB of disk for the virtual environment.
  Alternatively, use the standalone build (no Python needed).

## Install
From a PowerShell window in the project folder:
```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
```
Optional check that the camera works and which one LookFocus will use:
```powershell
.venv\Scripts\python.exe tools\cam_probe.py
```

## First run: calibrate
LookFocus has to learn how *your* head is oriented when you look at each display, which takes about a
minute. Calibration shows a dot on each display in turn (centre, left, right, top, bottom). Look at each dot
**the way you naturally would when working on that screen**: turn your head as you normally do, without
exaggerating and without using only your eyes.

### Two or more monitors
```powershell
.venv\Scripts\pythonw.exe -m lookfocus
```
This starts the tray app. The icon shows a red **!** because nothing is calibrated yet. Right-click it and
choose **Calibrate...**. Or, from a console:
```powershell
.venv\Scripts\lookfocus.exe calibrate
```

### One monitor (zone mode)
With a single monitor, LookFocus can split it into side-by-side **zones** that act like separate displays.
This is handy with two windows side by side on a wide screen.
```powershell
.venv\Scripts\lookfocus.exe calibrate --zones 2     # 2 to 8 zones; this also sets "zones" in the config
.venv\Scripts\pythonw.exe -m lookfocus
```
Without zones on a single monitor, LookFocus says there's only one display and does nothing. To set zones
from the tray instead, choose **Open config**, set `"zones": 2`, save, then choose **Calibrate...**.

At the end of calibration LookFocus checks that the displays can be told apart. If two of them are too
close together, it tells you so (see [Camera placement](#camera-placement-and-lighting)).

Recalibrate whenever you move the camera, the laptop or a monitor, or change the camera-sharing setting
(it changes the camera's resolution). LookFocus notices added or removed monitors and asks for it.

## Everyday use
Start the tray app with `.venv\Scripts\pythonw.exe -m lookfocus`, which opens no console window, or turn on
**Start with Windows**. The icon may first appear under the taskbar's **^** overflow; drag it onto the
taskbar to keep it visible.

| icon | meaning |
|---|---|
| green, eye | active: watching, and switching when you face another display |
| amber ring | no face in view (or the camera is starting); runs at ~2.5 fps and doesn't switch |
| grey, pause bars | paused: by you, while locked/asleep/screen off, or the camera was lent to another app. **The camera is off while paused.** |
| blue crosshair | calibrating |
| red ! | needs attention: not calibrated, only one display, display layout changed, or a failure (hover for details) |

Hovering shows the current state, for example "LookFocus: facing Zone 2/2". Left-click toggles
pause. Right-click menu:
- **Pause / Resume.** The pause hotkey **Ctrl+Alt+Shift+G** works from anywhere.
- **Calibrate / Recalibrate...** Press **Esc** to cancel; nothing changes until a calibration completes.
- **Choose camera.** Automatic, or a specific camera. Saved in the config.
- **Camera sharing...** Explains and opens Windows' "use the camera in several apps" setting (see below).
- **Open config.** Opens `config.json` in your editor. Saved changes apply within a few seconds.
- **Debug view.** A live window with the camera image, head angles and which display you're facing; useful
  for tuning. Close it with Esc or q.
- **Start with Windows** (a per-user sign-in entry, no admin rights needed) and **Quit**.

When it switches:
- You face another display for **0.3 s** (1 s if you typed in the last 2 s, so focus never jumps
  mid-word).
- Not while you're using the mouse, nor for 1.5 s after. Clicking is already a choice of window.
- Looking at your phone, the desk or out of the window ("none of the displays") keeps the current
  display. So does leaving the camera's view.
- The window that gets focus is the one you last used on that display, or the topmost one if that was
  closed. If the display has no window, nothing happens.
- A click or Alt-Tab of your own is never undone. LookFocus only acts when the display you face changes.

Pausing also happens automatically while the PC is locked, asleep or the screen is off, and switching
resumes afterwards. A manual pause stays paused until you resume it.

`lookfocus run` runs the same engine in a console, which is useful for testing changes. Add `--dry-run` to
decide without moving focus, or `--stats` for CPU and fps every 10 s.

## Camera placement and lighting
LookFocus can only tell displays apart if your head points in noticeably different directions for each one.
- **Keep the camera fixed** relative to your monitors. A laptop camera moves with the lid, so tilting the lid
  changes every angle. Recalibrate after moving anything.
- **Centre it.** The best spot is above the middle of your monitor arrangement, or above the display you
  use most. The camera must see your face when you look at every display: check with **Debug view** while
  you look at each corner.
- **Displays should be at least ~10° apart.** Two monitors side by side easily are. Zones on one laptop
  screen are borderline, because you hardly turn your head between the halves of a 15" screen at arm's
  length. Two zones on a 27"+ monitor work well. The calibration check warns when displays are too close.
  Turn your head a little more than you would for your eyes alone.
- **Distance:** roughly 40–100 cm, with your whole face in the frame. Glasses are fine. A hand on your
  chin, a mug or a mask can hide landmarks and cause brief "no face" readings, which don't switch
  anything.
- **Lighting:** light your face evenly from the front or the side of the screens. Avoid a bright window or
  lamp **behind** you, which turns your face into a silhouette. A dim room works if the camera still shows
  your face clearly, but noise goes up. If the debug view shows "camera image is dark", there's too little
  light or the lens is covered.
- **Privacy shutter:** if your camera has a physical or software shutter and it's closed, LookFocus reports
  "camera image is dark" or "frozen" and simply doesn't switch.
- **One face:** LookFocus tracks one face. If someone else is in view and closer to the camera, it may
  follow them.

## Tuning guide
Settings live in `%APPDATA%\LookFocus\config.json` (tray: **Open config**). Edit and save: LookFocus applies
the change within a few seconds. A bad value is reported in a notification and replaced by its default; it
never stops LookFocus. Use **Debug view** to see what LookFocus sees while you tune.

| symptom | try |
|---|---|
| switches on a quick glance | raise `dwell_ms` (300 → 500) |
| feels slow to switch | lower `dwell_ms` (→ 200); check `typing_dwell_ms` if it's slow only after typing |
| focus moves while you type and glance at notes on the other screen | raise `typing_dwell_ms` (1000 → 2000) or `typing_grace_ms` |
| flips back and forth when you look near the bezel | raise `hysteresis` (2 → 3–4) |
| reluctant to switch to the edge of a display | lower `hysteresis`, or recalibrate turning your head naturally |
| switches when you look at your phone or desk | lower `none_margin` (6 → 4) |
| ignores you when you look at a far corner of a display | raise `none_margin` (6 → 8–10) |
| switching right after using the mouse is too slow | lower `mouse_grace_ms` (1500 → 800) |
| CPU use too high | lower `fps` (15 → 10); about half the CPU goes to face tracking |
| a menu bar flashes in some app when focus arrives | set `allow_alt_fallback` to `false` |
| want the pointer to follow focus | `move_cursor_on_switch: true` |
| camera slow to start | `camera_backend: "dshow"` (the other backend is always tried as a fallback) |
| up/down (stacked monitors) unreliable | try `pose_source: "lm3d"` (no recalibration needed) |

All settings:

| setting | default | range | meaning |
|---|---|---|---|
| `dwell_ms` | 300 | 0–5000 | how long you must face another display before it gets focus |
| `typing_dwell_ms` | 1000 | 0–10000 | the dwell used right after typing |
| `typing_grace_ms` | 2000 | 0–30000 | "right after typing" = within this long of a keyboard event |
| `mouse_grace_ms` | 1500 | 0–30000 | no switching while the mouse moves or a button is down, and this long after |
| `hysteresis` | 2.0 | 0–50 | how much more clearly you must face a new display than the current one (normalized calibration units, roughly 1–2° each) |
| `none_margin` | 6.0 | 0.5–100 | how far outside every display a pose must be to count as "none" (same units) |
| `move_cursor_on_switch` | false | | also move the pointer to the newly focused display |
| `allow_alt_fallback` | true | | allow the last-resort Alt key tap when Windows refuses focus |
| `fps` | 15 | 1–30 | camera frames processed per second while a face is visible |
| `camera_backend` | "msmf" | msmf, dshow | Windows camera API tried first |
| `camera_index` / `camera_name` / `camera_path` | automatic | | the camera to use; set by **Choose camera** |
| `yield_camera_to_other_apps` | true | | release the camera when another app needs it (see below) |
| `zones` | 0 | 0, 2–8 | split the primary monitor into N side-by-side zones; 0 = one display per monitor |
| `pose_source` | "matrix" | matrix, lm3d, lm2d | which head-angle estimate to classify |
| `hotkey` | "ctrl+alt+shift+g" | | pause hotkey: modifiers (ctrl, alt, shift, win) + a letter, digit, F1–F24 or a named key such as `pause` or `space` |
| `notifications` | true | | tray notifications |
| `log_level` | "INFO" | DEBUG, INFO, WARNING, ERROR | log detail |

## Video calls and camera sharing
Without extra settings, Windows lets only one app at a time get images from a camera. LookFocus watches for
other apps opening the camera (a video call, the Camera app, a browser). When one does, LookFocus **releases
the camera**, shows a notification and resumes when that app is done.

Windows 11 24H2 (build 26100.3321) and later also have **"Allow multiple apps to use camera at the same
time"** under *Settings > Bluetooth & devices > Cameras > (your camera) > Advanced camera options*. With it
on, LookFocus and your video call both work at the same time. The tray's **Camera sharing...** item tells
you whether your Windows has it and opens the page. LookFocus also offers it once, the first time it finds
the setting off.

Note that with the setting on, Windows serves every app the resolution chosen in camera settings. Changing
the setting therefore changes what LookFocus sees, and LookFocus suggests recalibrating when that happens.

## Administrator windows
Windows doesn't let a normal program take keyboard focus away from a window running as administrator.
LookFocus can switch *to* an admin window, but not *away* from one. It notices this immediately, does nothing
and mentions it once per session. If you work in admin windows a lot, run
`.venv\Scripts\pythonw.exe -m lookfocus tray --elevated`. It asks for UAC approval at every start; Start with
Windows can't do this, so autostart always runs non-elevated.

## Troubleshooting
- **Red ! icon:** hover for the reason.
  - "not calibrated": choose Calibrate.
  - "only one display": set `zones` (see [One monitor](#one-monitor-zone-mode)).
  - "display layout changed": a monitor was added, removed or moved since calibration; recalibrate.
  - "stopped: ...": an internal failure. Check the log, then Quit and start again.
- **Doesn't switch:**
  - Open **Debug view**. Is your face detected (landmarks drawn)? Does "facing" change when you turn?
  - Not paused? (Grey icon. Try Ctrl+Alt+Shift+G.)
  - Did you use the mouse within 1.5 s?
  - Is there a window on the target display?
  - Is the current window an admin window (see above)?
- **Switches to the wrong display:** recalibrate, and check that the camera hasn't moved. If the
  calibration result warned that displays are close, move the camera to the centre, or turn your head more
  during calibration and use.
- **"Camera released for ..." notifications during calls:** expected with camera sharing off. Turn the
  setting on to use both.
- **Hotkey doesn't work:** another app may own the combination (LookFocus says so when it starts). Pick
  another `hotkey` in the config.
- **"LookFocus is already running":** only one copy runs at a time. Look for its tray icon, including under
  the **^** overflow.
- **Logs:** `%APPDATA%\LookFocus\logs\lookfocus.log` (rotated at 1 MB, 3 kept). Set `"log_level": "DEBUG"`
  for more detail.
- **Start over:** Quit, then delete `%APPDATA%\LookFocus` (config, calibration, logs).

## Known limitations
- **Head pose, not eye gaze.** You have to turn your head; eye-only glances aren't detected. This is by
  design, because head pose works with ordinary webcams.
- **Displays only.** LookFocus picks a display and focuses the last window used there. It doesn't choose
  between windows or panes on the same display.
- **Calibration is tied to the camera:** its position and its resolution mode (the camera-sharing setting).
  Change either and you need to recalibrate.
- **Scrolling with the mouse wheel counts as typing** (no pointer movement): it only lengthens the next
  dwell to 1 s.
- **Activation side effects.** Windows refuses focus requests from background programs, so LookFocus
  escalates. When a plain request is refused, it attaches to the input of the target's thread, then sends
  an unassigned virtual key tap, and as a last resort an Alt tap, which can flash an app's menu bar (turn
  off with `allow_alt_fallback`). On the test machine, Win32 apps (Explorer, VS Code, Terminal, Firefox)
  and Store apps (Calculator, Settings) all switched without reaching the Alt stage. The secure desktop
  (UAC prompts, the lock screen) can never be focused.
- **Elevated windows** need `--elevated`; see above. UIAccess isn't used.
- **Resource use** (test machine, 15 fps, face in view): 6–8% of total CPU and about 200 MB of memory
  (working set; ~270 MB committed), mostly mediapipe and OpenCV. With no face in view it uses about 1% CPU;
  while paused it uses none and the camera is off.
- **Untested (one-monitor development machine):**
  - two or more physical monitors, especially with **different DPI scaling** or **different
    resolutions**;
  - arrangement quirks: monitors stacked vertically, offset or with negative coordinates, portrait
    orientation, very different viewing distances;
  - plugging or unplugging monitors, or docking and undocking a laptop, while LookFocus runs (it's designed
    to pause and ask for recalibration);
  - external USB cameras, DirectShow-only cameras, several cameras at once;
  - Windows 10;
  - full-screen games and other exclusive-mode apps.

  Monitor layout, DPI handling and window-to-display mapping are unit-tested with synthetic layouts. Zone
  mode on one real monitor is tested end to end.

## Optional: standalone build
A folder with `LookFocus.exe` that runs without Python, built with PyInstaller (no installer):
```powershell
.venv\Scripts\python.exe -m pip install -r requirements-build.lock
.venv\Scripts\python.exe tools\build_exe.py
```
This produces `dist\LookFocus\` (~225 MB). The folder contains:
- `LookFocus.exe`, the tray app (same as `pythonw -m lookfocus`);
- `lookfocus-cli.exe`, the console commands (`lookfocus-cli calibrate --zones 2`, `run`, `watch`,
  `camera-sharing`);
- `_internal\`, which must stay next to them.

Copy the whole folder wherever you like. **Start with Windows** points at whichever copy you last started.
The build script checks the result: the model hash, mediapipe without network APIs, OpenCV's FFmpeg
plugin (network-capable, not needed for cameras) left out, and one face-tracker run inside the built exe.
matplotlib is bundled because mediapipe imports it. In the built app it rebuilds a small font list in a temp
folder at each start; Quit removes it.

The executables are **unsigned**:
- SmartScreen may say "Windows protected your PC" (choose *More info > Run anyway*).
- Antivirus products sometimes flag PyInstaller-built programs as suspicious, a known false positive
  caused by how its launcher unpacks Python.
- Building it yourself from this source is the way to be sure what's in it. If your antivirus quarantines
  it, add an exclusion for the folder or run from source instead.

## Uninstall
1. Untick **Start with Windows** (or delete the `LookFocus` value under
   `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`).
2. **Quit**.
3. Delete the project folder or `dist\LookFocus`, and `%APPDATA%\LookFocus`.

## Development
```powershell
.venv\Scripts\python.exe -m pytest                 # unit tests, hardware-free (includes privacy checks)
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
```
Tools:
- `tools\cam_probe.py`: cameras, backends and open latency.
- `tools\debug_pose.py [--guided]`: live head-pose viewer with its own camera.
- `tools\focus_test.py --zones 2 --windows A B`: measures the focus-activation ladder between two windows.
- `tools\cam_share_test.py`: can a second app use the camera at the same time?
- `tools\net_audit.py` (or `--pid N` to watch a running copy): records any network connection.
- `python -m lookfocus.camera_activity`: which processes use the camera.

Layout: `src/lookfocus/`. The pure, unit-tested logic (pose, displays, calibration, classifier, decision,
input tracker, targeting, config, camera-usage policy) is separate from the Windows parts (capture,
monitors, overlay, focus, tray, system events). `CLAUDE.md` has the conventions; `DECISIONS.md` has the why
and the measurements behind every choice. mediapipe must stay at 0.10.33; after any dependency change run
pytest and `tools\net_audit.py`.
