# LookFocus user guide

The [README](../README.md) covers the basics. This guide has the details: how switching decides, every setting,
troubleshooting, the standalone build and developer notes. `DECISIONS.md` records why each choice was made and
the measurements behind it.

- [Install](#install)
- [Calibration](#calibration)
- [When it switches](#when-it-switches)
- [Settings](#settings)
- [Video calls and camera sharing](#video-calls-and-camera-sharing)
- [Administrator windows](#administrator-windows)
- [Troubleshooting](#troubleshooting)
- [Privacy: how it's enforced](#privacy-how-its-enforced)
- [Resource use](#resource-use)
- [Known limitations](#known-limitations)
- [Standalone build](#standalone-build)
- [Uninstall](#uninstall)
- [Development](#development)

## Install
Requirements:
- Windows 11, x64. Windows 10 may work but hasn't been tested.
- A colour webcam roughly in front of you. A Windows Hello IR camera is detected and skipped automatically.

**Download (recommended):** get `LookFocus-<version>-win64.zip` from the releases page, unzip it anywhere and
double-click `LookFocus.exe`; see the [README](../README.md#get-started). The folder also has
`lookfocus-cli.exe` for the commands below (`lookfocus-cli calibrate --zones 2`, `lookfocus-cli run`, ...)
and a `README.txt`. It needs about 225 MB and no Python.

**From source** (for development, or if you'd rather not run a downloaded exe): Python 3.12 (x64) from
python.org, and about 700 MB of disk for the virtual environment.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
.venv\Scripts\python.exe tools\cam_probe.py        # optional: which cameras are there, which one is used
```

Start the tray app with `.venv\Scripts\pythonw.exe -m lookfocus` (no console window). `lookfocus run` runs
the same engine in a console, which is useful for testing. Add `--dry-run` to decide without moving focus, or
`--stats` for CPU and fps every 10 s.

## Calibration
A dot appears on each display in turn: centre, left, right, top and bottom. Each dot is yellow while you settle
in and turns green while LookFocus measures (about 2 s). Look at the dots the way you naturally would when
working on that screen: turn your head as you normally do, without exaggerating and without using only your
eyes. Press **Esc** to cancel; nothing changes until a calibration completes.

- **First run:** a welcome message offers to calibrate. On a single monitor it offers to split it into two
  zones first. It's shown once; afterwards the red tray icon and a notification say what's missing.
- **Several monitors:** tray → **Calibrate...**, or `lookfocus calibrate` in a console.
- **One monitor, zone mode:** tray → **Screen zones** → split in 2, 3 or 4. Calibration starts right away,
  because the old one can't fit new zones. From a console: `lookfocus calibrate --zones 2` (2 to 8 zones),
  which also sets `"zones"` in the config. Without zones on a single monitor, LookFocus reports that there's
  only one display and does nothing.
- At the end, LookFocus checks that the displays can be told apart and warns you when two are too close.
- **Recalibrate** whenever you move the camera, the laptop or a monitor, or change the camera-sharing setting
  (it changes the camera's resolution). LookFocus notices added or removed monitors and asks for it.

## When it switches
- You face another display for **0.3 s**, or 1 s if you typed in the last 2 s.
- Not while you use the mouse, nor for 1.5 s after.
- Looking at none of the displays (phone, desk, window) keeps the current one. So does leaving the camera's
  view; with no face for a few seconds LookFocus drops to ~2.5 fps until you're back.
- The window that gets focus is the one you last used on that display, or the topmost one if that was closed.
  No window there: nothing happens.
- Your own clicks and Alt-Tabs are never undone. LookFocus only acts when the display you face changes.
- Hysteresis: leaving the current display needs a slightly clearer turn than entering it, so looking near the
  bezel doesn't flip back and forth.

Pausing also happens automatically while the PC is locked, asleep or the screen is off. A manual pause (hotkey
or tray) stays paused until you resume it.

## Settings
`%APPDATA%\LookFocus\config.json` (tray: **Open config**). Edit and save: LookFocus applies the change within a
few seconds. A bad value is reported in a notification and replaced by its default; it never stops LookFocus.
Use **Debug view** to see what LookFocus sees while you tune.

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
| CPU use too high | lower `fps` (15 → 10) |
| a menu bar flashes in some app when focus arrives | set `allow_alt_fallback` to `false` |
| want the pointer to follow focus | `move_cursor_on_switch: true` |
| camera slow to start | `camera_backend: "dshow"` (the other backend is always tried as a fallback) |
| up/down (stacked monitors) unreliable | try `pose_source: "lm3d"` (no recalibration needed) |

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
| `yield_camera_to_other_apps` | true | | release the camera when another app needs it |
| `zones` | 0 | 0, 2–8 | split the primary monitor into N side-by-side zones; 0 = one display per monitor (tray: **Screen zones**) |
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
on, LookFocus and your call both work at the same time. Tray → **Camera sharing...** tells you whether your
Windows has it and opens the page. LookFocus also offers it once, the first time it finds the setting off.

With the setting on, Windows serves every app the resolution chosen in camera settings. Changing the setting
therefore changes what LookFocus sees, and LookFocus suggests recalibrating when that happens.

## Administrator windows
Windows doesn't let a normal program take keyboard focus away from a window running as administrator.
LookFocus can switch *to* an admin window (Task Manager is one), but not *away* from one. It notices
immediately, does nothing and mentions it once per session. If you work in admin windows a lot, right-click
`LookFocus.exe` → **Run as administrator** (from source: `.venv\Scripts\pythonw.exe -m lookfocus tray
--elevated`). It asks for UAC approval at every start; Start with Windows can't do this, so autostart always
runs non-elevated.

## Troubleshooting
- **Red ! icon:** hover for the reason.
  - "not calibrated": choose Calibrate.
  - "only one display": connect the other monitor, or split this one with **Screen zones**.
  - "display layout changed": a monitor was added, removed or moved since calibration; recalibrate.
  - "stopped: ...": an internal failure. LookFocus already restarted itself 3 times; check the log, then
    Quit and start again.
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
- **Logs:** `%APPDATA%\LookFocus\logs\lookfocus.log` (rotated at 1 MB, 3 kept). They're text only: state
  changes, the display you faced and the window that got focus, with titles cut to 40 characters. Set
  `"log_level": "DEBUG"` for more detail.
- **Start over:** Quit, then delete `%APPDATA%\LookFocus` (config, calibration, logs).

## Privacy: how it's enforced
- Frames exist only in memory. Nothing in LookFocus writes images to disk or the log.
- No network at runtime. mediapipe is pinned to 0.10.33, because later releases include a usage-telemetry
  uploader (`DECISIONS.md` D-003). At startup LookFocus refuses to start face tracking if the mediapipe native
  library links any network API or the face model's SHA-256 is wrong. `tools\net_audit.py` (or
  `--pid N` for a running copy) records any real connection.
- No hooks and no key identities: typing is inferred from `GetLastInputInfo` (some input happened) versus the
  pointer position and mouse-button state.

## Resource use
On the test machine (ThinkPad X9 15, 15 fps, face in view): 6–8% of total CPU and about 200 MB of memory
(working set; ~270 MB committed), mostly mediapipe and OpenCV. With no face in view about 1% CPU; while paused
none, and the camera is off.

## Known limitations
- **Head pose, not eye gaze.** You have to turn your head; eye-only glances aren't detected. This is by
  design, because head pose works with ordinary webcams.
- **Displays only.** LookFocus picks a display and focuses the last window used there. It doesn't choose
  between windows or panes on the same display.
- **Calibration is tied to the camera:** its position and its resolution mode (the camera-sharing setting).
- **Scrolling with the mouse wheel counts as typing** (no pointer movement): it only lengthens the next
  dwell to 1 s.
- **Activation side effects.** Windows refuses focus requests from background programs, so LookFocus
  escalates. When a plain request is refused, it attaches to the input of the target's thread, then sends
  an unassigned virtual key tap, and as a last resort an Alt tap, which can flash an app's menu bar (turn
  off with `allow_alt_fallback`). On the test machine, Win32 apps (Explorer, VS Code, Terminal, Firefox)
  and Store apps (Calculator, Settings) all switched without reaching the Alt stage. The secure desktop
  (UAC prompts, the lock screen) can never be focused.
- **Elevated windows** need `--elevated`; see above. UIAccess isn't used.
- **One face.** If someone else is closer to the camera, LookFocus may follow them.
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

## Standalone build
The release zip is built from source with PyInstaller (a one-folder build, no installer). From a source
install:
```powershell
.venv\Scripts\python.exe -m pip install -r requirements-build.lock
.venv\Scripts\python.exe tools\build_exe.py
```
This takes about two minutes. It produces `dist\LookFocus\` (~225 MB) and **`dist\LookFocus-<version>-win64.zip`**
(~95 MB, the file to attach to a release). The folder contains:
- `LookFocus.exe`, the tray app (same as `pythonw -m lookfocus`), with version info and the LookFocus icon;
- `lookfocus-cli.exe`, the console commands (`lookfocus-cli calibrate --zones 2`, `run`, `watch`,
  `camera-sharing`);
- `_internal\`, which must stay next to them;
- `README.txt`, a plain-text quick start for people who downloaded the zip;
- `third-party-licenses\`, the license files of every bundled library plus Python and PyInstaller's
  bootloader, with an index. pystray (LGPL-3.0) ships as plain `.py` files in `_internal\pystray` so it can be
  replaced.

Copy the whole folder wherever you like. **Start with Windows** points at whichever copy you last started.

The build script checks the result and makes the zip only if everything passes:
- the model hash;
- mediapipe without network APIs;
- OpenCV's FFmpeg plugin (network-capable, not needed for cameras) left out;
- pystray shipped as `.py` files;
- license files present;
- `--help`, and one face-tracker run inside the built exe (`--self-check`).

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
3. Delete the LookFocus folder (the unzipped download, or the source folder) and `%APPDATA%\LookFocus`.
   Nothing else was installed.

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

The README video was made with the `/brag` skill (Hyperframes). Its source lives in `brag-output/`, which is
not committed (it holds a copy of the music track).
