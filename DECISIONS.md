# Decisions

Each entry: what was decided, why, and the evidence. Newest milestone last.
Status markers: **[verified]** = checked on the dev machine; **[unverified]** = reasoned but not yet tested.

---

## M0 — environment probe (2026-09-30)

Dev machine: Windows 11 Pro 10.0.26200, AMD64, one camera ("Intel(R) LNL AVStream Camera", reported to
apps as "Integrated Camera"; Lunar Lake IPU/MIPI sensor). Python on the machine: 3.14.7 (default), 3.12.10,
3.13.12 (a StabilityMatrix-bundled copy, not ours).

### D-001 Python 3.12 **[verified]**
- mediapipe's PyPI classifiers list 3.9–3.12 for every recent release (0.10.30 … 1.0.1). Since 0.10.30 the
  Windows wheel is `py3-none-win_amd64` with no `requires_python`, so pip would install it on 3.14, but 3.12
  is the newest version upstream claims to support.
- numpy 2.5.3 requires Python >= 3.12, which rules out 3.11 unless numpy is held back.
- 3.12.10 is already installed (`py -3.12`). → **Python 3.12**; `requires-python = "==3.12.*"`.

### D-002 venv + pip, not uv **[verified]**
- uv is not installed on the machine, and I did not want to add system-wide tooling without being asked.
  Stdlib `venv` + pip is enough for one app.
- Pinning: exact `==` pins for direct deps in `pyproject.toml`; the full transitive set in `requirements.lock`
  (`pip freeze --exclude-editable`). Build backend pinned (`setuptools==84.0.0`).
- If uv is wanted later: `uv venv -p 3.12 && uv pip sync requirements.lock` works on the same lock.

### D-003 mediapipe **0.10.33**, not the latest 1.0.1 — telemetry **[verified]**
The biggest M0 finding. **mediapipe >= 0.10.35 sends usage telemetry to Google at runtime**, which breaks the
"no network at runtime" invariant.
- Google's MediaPipe Tasks privacy notice (last modified 2026-06-05): *"MediaPipe Tasks APIs send metrics about
  the performance and utilization of the APIs in your app to Google."* It names no platforms and gives no
  opt-out. <https://developers.google.com/edge/mediapipe/solutions/tasks>
- Static evidence: `mediapipe/tasks/c/libmediapipe.dll` in 1.0.1 imports `WININET.dll`
  (`InternetOpenA`, `InternetConnectA`, `HttpOpenRequestA`, `HttpSendRequestA`, …) and contains
  `mediapipe::tasks::core::logging::google_internal::ClearcutLoggingClient`, `PortableClearcutUploader`,
  `https://play.googleapis.com/log`, and "Timed out waiting for Clearcut upload to complete."
- Dynamic evidence (`tools/net_audit.py`, which polls this process's TCP table every 10 ms): with 1.0.1, **at
  `FaceLandmarker.close()` the process opened `10.2.0.2:53702 -> 172.217.118.4:443`** (SYN_SENT, then
  ESTABLISHED). Nothing connected during import, model load or 30 s of inference.
- The same scan of every Windows wheel since the last pybind build:

  | version | network DLL imports | Clearcut markers |
  |---|---|---|
  | 0.10.21 (pybind, cp312) | none | none |
  | 0.10.30 / .31 / .32 / **.33** (C API, py3-none) | none | none |
  | 0.10.35 | WININET | yes |
  | 1.0.0 / 1.0.1 | WININET | yes |

  (There is no 0.10.34 on PyPI.)
- 0.10.33 with `tools/net_audit.py --seconds 60 --tail 20` (import, create, 714 frames of inference, close,
  then 20 s idle): **no TCP connections**.
- Upstream issue, open with no maintainer answer: google-ai-edge/mediapipe#6291 "Mediapipe now includes
  undocumented telemetry". Its reporter's version bounds match mine.
- 0.10.33 has the same Tasks API we need (`mp.tasks.vision.FaceLandmarker`, `FaceLandmarkerOptions` with
  `num_faces`, `output_facial_transformation_matrixes`, `RunningMode.VIDEO`, `detect_for_video`), works with
  numpy 2.5.3, and was released 2026-03-18.
- **Guards:** `tests/test_privacy.py` parses the PE import tables of the *installed* mediapipe binaries and
  fails if any links a network DLL; I confirmed it flags the 0.10.35 and 1.0.1 DLLs. `tools/net_audit.py`
  is the runtime check to re-run after any dependency change. The app will run the same static check at
  startup and refuse to start if it fails (wired in M4).
- Rejected alternatives: patching WinINet imports inside the DLL (import hooking plus runtime code patching
  looks like malware to antivirus and is fragile); third-party rebuilds of libmediapipe (supply-chain risk);
  a firewall rule (needs admin, and the venv `python.exe` is a launcher for the base interpreter, so a
  per-exe rule catches the wrong binary). A firewall rule is still a reasonable extra layer for a
  PyInstaller build and will be documented in the README.
- Revisit if Google ships an opt-out or a clean build. **Do not bump mediapipe without re-running both guards.**

### D-004 OpenCV: `opencv-contrib-python==5.0.0.93`, not `opencv-python` **[verified]**
- mediapipe 0.10.33 declares `opencv-contrib-python` as a dependency. Installing `opencv-python` as well
  would put two packages in the same `cv2/` directory (a known breakage source), so we depend on the contrib
  build directly and pin it.
- 5.0.0.93 is a cp37-abi3 win_amd64 wheel; `cv2.CAP_MSMF`, `cv2.CAP_DSHOW`, the `VideoCapture(index, api,
  params)` constructor and `cv2.videoio_registry` all exist. Camera backends compiled in: GSTREAMER, MSMF, DSHOW,
  FFMPEG, UEYE, OBSENSOR.
- mediapipe imports cv2 itself, so `OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0` is set in
  `lookfocus/__init__.py`, which runs before any lookfocus submodule, and tools import `lookfocus` before
  `cv2`/`mediapipe` (with an `# isort: split` marker so ruff can't reorder it).

### D-005 Camera backend: MSMF default, DSHOW fallback; resolution via open params **[verified on this camera]**
`tools/cam_probe.py --compare-set` on the integrated camera:

| backend:index | mode | open | first frame | delivered |
|---|---|---|---|---|
| msmf:0 | open params (640×480) | 0.52 s | 0.50 s | 30.3 fps, 640×480 colour |
| msmf:0 | no params | 0.68 s | 0.29 s | 29.4 fps (640×480 is this camera's default) |
| msmf:0 | open, then `set()` ×3 | 0.46 s | 0.36 s | set() cost 0.03 s total |
| dshow:0 | open params | 1.05 s | 0.06 s | 30.0 fps, YUY2 |
| dshow:0 | open, then `set()` ×3 | 0.78 s | 0.06 s | **set() cost 2.18 s** (height 1.13 s, fps 1.05 s) |
| dshow:1 (OBS Virtual Camera) | any | fail | — | OBS not running; expected |

- Both backends work on this camera and open in about 1 s. MSMF is the default because it is the modern
  path, it is the only one that sees Frame Server–shared cameras, and the DSHOW crash reports mentioned in the
  brief. DSHOW is the automatic fallback. Both are configurable.
- Resolution goes through the `VideoCapture(index, api, [PROP, value, …])` open params, applied once during
  negotiation, never as `cap.set()` calls. The ~1 s per `set()` on DSHOW here confirms that advice.
- The camera delivers 30 fps natively; LookFocus throttles to <= 15 fps in software (grab and drop).
  Whether requesting 15 fps in the open params reduces camera power without slowing the open is untested (M1).
- Every open runs on a worker thread with a timeout (default 8 s). If the timeout fires and the open later
  succeeds, the worker releases the device itself so it is never leaked.

### D-006 Win32 access: **ctypes only**, no pywin32 **[verified]**
Checked by installing `pywin32==312` (current, cp312 wheel available) into a throwaway venv and testing
`hasattr` for every Win32 call LookFocus needs. pywin32 312 does **not** expose:
`SendInput`, `DwmGetWindowAttribute` (needed for `DWMWA_CLOAKED`), `SetProcessDpiAwarenessContext`,
`SetProcessDPIAware`, `GetDpiForMonitor`, `RegisterPowerSettingNotification`. All of these are required.
Media Foundation / DirectShow device enumeration isn't in pywin32 either. It does cover the rest
(`AttachThreadInput`, `GetLastInputInfo`, `EnumDisplayMonitors`, `RegisterHotKey`,
`WTSRegisterSessionNotification`, …).

So ctypes is needed anyway. Using it for everything means one binding style, no ~7 MB compiled dependency,
explicit `argtypes`/`restype` (no hidden conversions), and a simpler PyInstaller build. pystray's Windows
backend is ctypes as well.

### D-007 Tray: `pystray==0.19.5` + `Pillow==12.3.0` **[partly verified]**
- 0.19.5 (2023-09-17) is still the latest release. Its Windows backend is pure ctypes
  (`pystray/_win32.py`) and declares `HAS_MENU`, `HAS_MENU_RADIO`, `HAS_NOTIFICATION`, `HAS_DEFAULT_ACTION`;
  it supports `run_detached()` and runs its own message loop. Nothing better maintained exists that doesn't
  pull in a GUI toolkit. The tray itself isn't exercised until M5.

### D-008 Model file committed to the repo, hash-checked **[verified]**
Downloaded `face_landmarker.task` from the official URL on the Face Landmarker docs page (details and hash in
`models/SOURCE.md`). The URL is a moving `latest` alias, so the exact bytes are committed (3.7 MB, Apache-2.0)
and checked by sha256 in tests. That keeps setup offline-reproducible, and the app never needs to download
anything.

### D-009 Camera identity = device path, not just index **[verified enumeration; policy for M1+]**
OpenCV indexes cameras by position only. `lookfocus/camera_enum.py` walks the same lists OpenCV uses
(`MFEnumDeviceSources(VIDCAP)` for MSMF, `CLSID_VideoInputDeviceCategory` for DSHOW) through ctypes COM, so
each index gets a friendly name and a stable symbolic link / DevicePath. On this machine MSMF sees 1 device
and DSHOW sees 2 (plus OBS Virtual Camera, which has no DevicePath), so **indices differ between backends**.
Config will store backend + device path + name and re-resolve the index at open.

### D-010 IR / greyscale camera skipping by frame content **[partly verified]**
No IR camera is enumerated on this machine by either backend. Windows Hello IR sensors usually register under
KSCATEGORY_SENSOR_CAMERA, which the default VIDCAP enumeration excludes, but some appear as ordinary cameras.
Detection therefore looks at frames: `capture.frame_stats()` computes mean |B−G| + |G−R| on a subsampled frame.
Greyscale means chroma < 1.5 and not dark. A mean luma < 12 is reported as **dark** (covered lens, or the
ThinkPad privacy shutter) rather than greyscale, so a closed shutter isn't mistaken for an IR camera. A name
hint (`IR`, `infrared`, `Hello`) is shown too. This camera measured chroma about 35 at luma about 160 (colour).
The greyscale path is **[unverified]** without an IR device.

Follow-up after the user's M0 checks:
- The user confirmed Windows Hello face sign-in is set up, so an IR sensor exists. Neither the MSMF
  (VIDCAP) nor the DSHOW enumeration lists it, so LookFocus never opens it. **[verified]**
- With this ThinkPad's **software privacy shutter** on, the camera keeps delivering 30 fps of flat grey
  frames (luma 140, chroma 4.0, down from about 35). It classified as "colour". That's harmless: the face
  tracker sees no face, so LookFocus goes to no-face mode (switching disabled, low frame rate), which is the
  right behaviour. To tell the user *why*, cam_probe and the capture thread now also measure frame-to-frame
  motion: a synthetic placeholder has none (motion < 0.25), while a live sensor has noise even in a dark room
  (measured 0.29 at luma 2). Whether the shutter image is truly static is **[unverified]** until the user
  re-runs cam_probe with the shutter on.

## Post-M0 findings

### D-011 The camera is exclusive: a second app gets no frames **[verified]**
`tools/cam_share_test.py` holds the camera in one process and opens it from a second process:

| holder | second app via | second app's open | second app's frames | holder |
|---|---|---|---|---|
| msmf | msmf | "succeeds" (0.49 s) | **0** (79 failed reads in 4 s) | unaffected |
| msmf | dshow | "succeeds" | 4 blank frames (chroma 0.0) | unaffected |
| dshow | msmf / dshow | same as above | same | unaffected |

So **while LookFocus holds the camera, a video-call app gets no video** (and vice versa). The Windows Camera
app failed during the user's test with `0xA00F429F <WindowShowFailed> (0x80070057 E_INVALIDARG)`. That's the
Camera app failing to create its own window, not a "camera in use" error, so the test was inconclusive and
cam_share_test replaces it.
- Windows 11 has a per-camera setting, *Settings → Bluetooth & devices → Cameras → (camera) → Advanced camera
  options → "Allow multiple apps to use camera at the same time"*, added in 24H2 26100.8524 / 25H2 26200.8524.
  This machine is on **26200.9457**, so it should be present. Whether it makes both apps get frames is
  **[unverified]**; the user should enable it and re-run cam_share_test.
- Built into the capture code regardless:
  1. **Pause releases the camera** (`CaptureThread.set_paused`), so the pause hotkey frees it for a call.
  2. An open that "succeeds" but yields no frames within 3 s counts as a failed open, and LookFocus retries
     with backoff (1, 2, 5, 10, 30 s). Started during a call, it waits quietly instead of hanging, and the
     retries don't disturb the app that holds the camera (the holder saw 0 failed reads).
- Open question for the user: should LookFocus **auto-yield** the camera when another app starts using it?
  The camera privacy store (`HKCU\...\CapabilityAccessManager\ConsentStore\webcam`, per-exe `LastUsedTimeStop
  == 0` means in use) is a plausible signal. It's keyed by executable path, and whether a *blocked* app
  registers there is **[unverified]**. It's not in the spec, so it isn't built.

## M1 — head pose and the debug viewer

### D-012 Reading fresh frames: drain the backend's buffer **[verified]**
Timing successive reads after a pause shows buffered frames: MSMF returns ~2 frames instantly (2–3 ms) and
then blocks until the next sensor frame; DSHOW returns 1 instantly. Sleeping and then reading once would
therefore process a frame up to one pause old. `CameraSource.read_fresh()` calls `grab()` until one takes
longer than 8 ms (i.e. came from the sensor), at most 4 times, then `retrieve()`s it. The capture loop sleeps
until the next frame is due (15 fps active, 2.5 fps after 3 s without a face) and then reads fresh.
Other measurements on this camera (dark room, so auto-exposure was slow):
- MSMF ignores a 15 fps request in the open params and still delivers 30 fps; DSHOW ran at 15 fps in the dark.
- Reading alone at 30 fps costs 11–17% of one core on MSMF (software colour conversion, since the brief
  mandates HW transforms off) and about 4% on DSHOW.
- Capture thread, no face (dark), 15 fps: **34.7% of one core = 4.3% of the machine** (8 logical CPUs).
  No-face idle mode (~3 fps): 7.3% of one core = 0.9%. Numbers with a face tracked come in M1 hardware
  testing and M4.

### D-013 Pose estimators: sign-agnostic, three candidates side by side **[unit-tested; hardware pending]**
- The matrix layout is detected per matrix from which of the last row/column is (0,0,0,1). mediapipe
  0.10.33's `MatrixC.to_numpy()` claims to return row-major, but that isn't relied on. Scale/shear is removed
  by taking the nearest rotation (SVD).
- **matrix** yaw/pitch come from the face model's depth (z) axis, flipped into the camera's +z half-space
  before `atan2`. A face always points roughly along the camera axis with an unknown sign, so this is
  continuous around frontal (never wraps at ±180°) under any axis sign convention. For R = Ry·Rx·Rz it
  equals the Y-X-Z Euler yaw/pitch, which by construction ignore roll. Roll = Euler roll folded into
  (−90°, 90°]; its sign is convention-dependent, and that's fine because it's diagnostic only. Tests cover
  both model-side and camera-side (OpenGL vs OpenCV) convention flips.
- **lm3d**: the same angles from a face frame built from 3D landmarks (cheek 234→454, chin 152→forehead 10),
  independent of the matrix. This is the planned fallback if matrix pitch misbehaves.
- **lm2d**: unitless ratios of where the nose tip (1) sits between the cheeks and between the nasion (168) and
  chin (152). No depth needed.
- The classifier never assumes signs: calibration records whatever each estimator produces per monitor.
  **Which estimator feeds the classifier is decided from the user's guided check** in `debug_pose.py`
  (per-estimator span, SNR, crosstalk, tilt leak, repeatability).

### D-014 Capture thread **[verified without a face; with a face pending]**
- `CaptureThread` owns camera and tracker and publishes a `PoseSample` per processed frame to a
  `LatestSlot` (one value, overwritten, with a condition variable so readers wait for something newer;
  never a queue, so stale poses are dropped). Statuses: opening, tracking, lost (no face for < 3 s,
  still 15 fps), no_face (3 s+, 2.5 fps), error (with backoff retry), paused (camera released), stopped.
  Verified in a headless run: opening → lost → no_face at ~3 s → frame rate converging to ~3 fps;
  pause → camera released; resume → reopened; stop → thread exits.
- `FaceTracker` runs `privacy.verify_runtime()` (mediapipe network-import check plus model hash) before
  mediapipe's native library loads, and refuses to start if either fails.
- The camera choice is stored as device path / name / index. `device_key()` strips the interface-class GUID
  so a path recorded under MSMF matches the same device under DSHOW. If a configured camera is missing,
  LookFocus falls back to automatic choice (IR-named devices last) and says so.
- If the image explains a missing face, a hint is attached: "dark (lens covered or lights off?)" or "frozen
  (software privacy shutter on?)".

## M1 results and camera sharing (user-requested additions)

### D-015 Camera sharing: detect the setting, offer it at setup, yield when it's off **[detection superseded by D-023]**
The user asked for: (1) an optional setup step that checks whether this Windows build has "Allow multiple apps
to use camera at the same time" and offers to turn it on; (2) when another app starts using the camera and the
setting is off, LookFocus releases the camera and shows a notification that says so and suggests the setting
(if the build has it).
- With the setting ON, `cam_share_test` shows both apps get frames (MSMF second client 120 frames, DSHOW 119;
  holder unaffected). The second client's chroma of 115–127 worried me (possible colour mis-conversion), but a
  side-by-side check shows holder and second client both at chroma 54–74 with natural B/G/R means. It's just
  today's scene, not a conversion problem.
- **Availability:** Windows build >= 26100.3321 (the setting appeared in 24H2 March 2025; the Settings page
  moved under "Advanced camera options" in 26100/26200.8524). This is a heuristic: a build can have it staged.
  This machine: 26200.9457 → available.
- **State:** there's no API or documented registry value. Microsoft's Q&A answer (2025-09) says to test
  behaviourally. `camera_sharing.probe_while_holding()` runs a child process (the same executable, so the
  camera privacy store attributes it to LookFocus itself) that opens the camera while LookFocus holds it, and
  reports whether real frames arrive (≥ 5 frames that are lit or have sensor noise; a blocked client gets none,
  or all-black frames). The capture thread runs it after **every** camera open, because toggling the setting
  re-publishes the camera (all sensor groups re-published at 12:43:13, right after the user flipped it), so
  a stale result can't linger. A result is discarded if the camera was released while the probe ran.
  Verified: ON in 2.8 s. The OFF result is **[unverified]** since the setting is now on.
- **Hint only (turned out meaningless, D-023):** the Frame Server sensor-group key for this camera
  (`HKLM\SOFTWARE\Microsoft\Windows Media Foundation\FrameServer\SensorGroups\<hash>`) carries an undocumented
  `MEPSGFlag=1`; the IR sensor groups don't. It's logged next to each probe so its meaning can be confirmed,
  never trusted on its own.
- **Other-app detection (SUPERSEDED by D-023: blind to blocked apps):** the camera privacy store `HKCU\...\CapabilityAccessManager\ConsentStore\webcam`
  (packaged apps by family name, desktop apps under `NonPackaged\<exe path>`, `LastUsedTimeStop == 0` =
  in use). `UsageWatcher` waits on `RegNotifyChangeKeyValue` for the whole subtree (event-driven, plus a 5 s
  rescan), and diffs sessions by `LastUsedTimeStart` so only *new* sessions count. LookFocus's own executable
  (and the venv's base interpreter) is excluded. Limits: entries are per app, not per camera (an app using a
  different camera also triggers a yield), and every process of one executable shares an entry.
- **Policy** (`YieldPolicy`, pure and unit tested): yield when a new session starts and sharing isn't known to
  be ON; resume 2 s after every blocking app has stopped; a manual resume waives the current session of those
  apps only. If LookFocus starts while another app already holds the camera (open gives no frames and the store
  shows another app in use), it waits for that app ("busy") instead of retrying every 1–30 s.
- **Notifications:** the capture thread emits `yielded` / `busy` / `resumed` events with the title and body.
  Bodies fit the tray-balloon limits (63 / 255 characters) and mention the setting only if the build has it.
  They're shown through the tray icon in M5; until then `debug_pose.py` prints them. Example:
  "Camera released for Microsoft Teams" / "Microsoft Teams started using the camera, so LookFocus released it.
  It resumes when Microsoft Teams is done. To use both at once, turn on 'Allow multiple apps to use camera at
  the same time' in Settings > Bluetooth & devices > Cameras > Integrated Camera > Advanced camera options."
- **Verified end to end** with a genuinely different executable (a throwaway Python 3.14 + OpenCV "other app";
  its MSMF backend wouldn't open on this machine, so it used DSHOW):
  - sharing treated as off: other app starts at t=4.00 s → LookFocus yields at ~4.4–4.5 s (event carries the
    notification text) → the other app gets 177 frames → it stops at 10.4 s → LookFocus resumes at ~13.5 s
    and reopens.
  - real probe (ON): the other app starts → "sharing is on, keeping it" → the other app gets 178 frames while
    LookFocus keeps streaming.
- **The key open question, answered NO in D-023:** with the setting OFF, does a *blocked* app (open succeeds, no frames)
  register in the privacy store? The Python 3.14 stand-in only registered once it actually streamed (it never
  appeared while its MSMF open was failing). If blocked apps don't register, detection can't fire, and the pause
  hotkey is the fallback. The user needs to test this with the setting temporarily off.
- **Setup step:** `camera_sharing.run_setup_step()` reports availability, probes, and if OFF explains what that
  means, shows the Settings path, offers to open `ms-settings:camera`, and re-checks. It's the first step of
  `lookfocus calibrate` and also runs as `lookfocus camera-sharing` (and `python -m lookfocus.camera_sharing`).

### D-016 Classifier input: matrix yaw/pitch **[verified on the user's hardware]**
From the user's guided check (median per position, sd at centre):

| estimator axis | span | SNR | crosstalk | tilt leak | repeat |
|---|---|---|---|---|---|
| matrix horiz | 40.3° | 38 | 0.02 | 0.24 | 0.02 |
| matrix vert | 48.3° | 243 | 0.09 | 0.05 | 0.05 |
| lm3d horiz | 44.9° | 36 | 0.00 | 0.26 | 0.03 |
| lm3d vert | 54.8° | 265 | 0.05 | 0.06 | 0.04 |
| lm2d horiz | 0.67 | 49 | 0.04 | 0.12 | 0.02 |
| lm2d vert | 0.22 | 138 | 0.20 | 0.01 | 0.08 |

- **Matrix pitch is fine on this hardware**: monotonic, sd 0.2°, 48° span, agrees with the independent lm3d
  pitch at the extremes (UP −37.3 vs −37.3). No need to fall back to landmark pitch. The matrix layout
  detected as `row` (checked separately).
- Yaw is noisier at centre (sd ~1.0–1.3° in both estimators, so it's head micro-motion or shared landmark
  jitter, not a matrix artefact). SNR 38 is ample; the dwell and median filtering absorb it.
- Tilting the head ~18–22° moved yaw ~10° in *all* estimators (tilt leak 0.24). That looks like real head
  motion when tilting, not an estimator flaw. Casual tilts are much smaller.
- Left/right and up/down are asymmetric (+29/−11° yaw, −37/+11° pitch). That's the user's turns, and it
  doesn't matter because calibration records whatever each monitor produces.
- → default `pose_source = "matrix"`; `lm3d` stays selectable in config as a fallback.

### D-017 CPU with a face tracked: 8.8% of the machine at 15 fps — must come down (M4) **[measured]**
Headless capture thread, face tracked for 20 s: **70.5% of one core = 8.8% of 8 CPUs**, inference median
24.6 ms (p90 28.9 ms) versus 2–4 ms detection-only with no face. That misses "well under 10%". Profiling
without a face showed a ~3 ms `mp.Image` construction cost, ~0.8 ms `cvtColor`, and MSMF's 30 fps software
colour conversion (11–17% of a core). Options to measure in M4 with a face present: process at 10–12 fps
(a 300 ms dwell still gets 3–4 samples), cheaper DSHOW reads, and cutting per-frame copies.

## M2 — calibration and classifier

### D-018 Logical displays and monitor identity **[verified on one monitor; multi-monitor untested]**
- `displays.build_displays()` (pure) turns monitors into logical displays ordered left to right.
  `--zones N` (N ≥ 2) splits the **primary** monitor into N side-by-side strips with integer boundaries that
  cover it exactly; other monitors stay whole. With one display and no zones, LookFocus prints a warning
  suggesting `--zones 2` and exits with code 2.
- Monitor identity is `<monitor device path>|<W>x<H>@<x>,<y>` (the brief: "device name, resolution and
  position"). The device path comes from `QueryDisplayConfig`/`DisplayConfigGetDeviceInfo`
  (`monitorDevicePath`), which is stable per monitor and port, unlike the `\.\DISPLAYn` GDI names, which
  Windows can renumber. The GDI name is the fallback. A zone id appends `#zone<i>of<N>`, so changing the zone
  count also counts as a layout change.
- Per-monitor-v2 DPI awareness is set first thing in every command (`SetProcessDpiAwarenessContext(-4)`,
  with fallbacks). Verified: the built-in 2880×1800 panel reports physical rects at 192 DPI (200%). The
  internal panel has no friendly name, so it's labelled "built-in display".
- Window-to-display mapping (for M3/M4) is by window centre: the containing display, else the nearest one
  (a centre just off-screen).

### D-019 Calibration procedure **[verified with a synthetic closed loop; real head pending]**
- Five dots per display: centre, and 6% / 8% in from the left/right and top/bottom edges (fully visible
  and away from rounded corners and the taskbar). Each dot: 0.9 s to settle (the dot is yellow), then 2 s of
  collection (green, with a progress bar), about 30 samples at 15 fps.
- Per dot and per axis: median, then drop samples more than 3 robust sd (1.4826·MAD) away, then median
  again. A dot is redone (up to 2 retries) if fewer than 12 samples had a face, or if the robust sd exceeds
  4°. The user is told why on the overlay.
- Medians are stored for every estimator (matrix, lm3d, lm2d), so `pose_source` can change without
  recalibrating. JSON with version, zones and per-dot n/spread, written atomically (temp file +
  `os.replace`) to `%APPDATA%\LookFocus\calibration.json`.
- Layout change: `Calibration.check_layout()` diffs the calibrated display ids against the current ones.
  `watch` lists missing/new displays and tells the user to recalibrate (M5 will prompt from the tray, and
  M4 will react to `WM_DISPLAYCHANGE`).
- The calibration explicitly asks users to turn their head "naturally, as you would when working
  there". LookFocus tracks head pose, not eyes, so calibrating with exaggerated turns (or eyes only) would
  mismatch real use.
- The camera-sharing setup step (D-015) runs first unless `--skip-camera-sharing` is given.
- Closed-loop test: a synthetic "user" thread publishes the pose matching whatever dot the overlay shows. The
  full flow (both zones, 5 dots each, save/load, classify, layout check) completed in 12.1 s with short
  timings.

### D-020 Classifier: nearest calibrated centroid + region-based "none" **[unit-tested]**
- **Binding requirement honoured:** nearest centroid with normalized distance. Every calibration dot's
  median is a prototype for its display. With edge dots, the boundary between two adjacent displays is the
  bisector between one's edge dot and the neighbour's, i.e. at the bezel, even for displays of very
  different sizes. A test shows plain centre-to-centre classification would be wrong there.
- Normalization per axis: max(pooled calibration noise, 3% of the dots' extent on that axis). Pure noise
  normalization would let pitch (noise 0.2°) swamp yaw (noise ~1°); the extent floor keeps both axes
  comparable and works for unitless estimators.
- **"none"**: each display's region is the least-squares affine map from screen fraction to pose fitted to
  its 5 dots, applied to the whole rectangle. A pose more than `none_margin` (default 6 normalized units,
  ~6–12°) outside **every** region is "none". A threshold on the distance to the nearest centroid can't
  do this well, because a pose in a large display's corner is legitimately far from all its dots. Displays
  with fewer than 3 usable dots fall back to "distance beyond the farthest dot".
- Results carry per-display distances (for M4's hysteresis) and the position within the chosen display.
- Tests: centres, edges and corners; the bezel boundary; unequal sizes; vertically stacked displays; three
  noisy zones; "none" far below, to the side, and just outside; margin units; yaw/pitch sign flips give
  identical results (no sign conventions); NaN; degenerate displays.

### D-021 Calibration overlay **[verified on this machine]**
Raw Win32 via ctypes: a `WS_POPUP` topmost tool window (no taskbar button) sized to the logical display in
physical pixels, double-buffered GDI, sizes scaled by `GetDpiForWindow`, pointer hidden over it. It runs on
the main thread, pumped by the calibration loop (`MsgWaitForMultipleObjects`, no GUI thread, no busy loop).
Verified by reading back pixels of its own window on both zones (white dot core, background colour
elsewhere). `SetForegroundWindow` for the overlay was refused when launched from a console (foreground lock),
so the overlay text says **Ctrl+C in the terminal** cancels (Esc works if the overlay has focus).

### D-022 Separability check after calibration **[unit-tested]**
Leave-one-out over *all* dots is wrong here: adjacent displays' edge dots always sit close together across
the bezel, so it flagged a perfectly good synthetic calibration. The check instead reports centre-to-centre
distances and flags a problem when (a) a display's centre, left out, is classified onto another display,
(b) it lies inside another display's region, or (c) two centres are less than 6× the calibration noise apart
(this catches "calibrated with the eyes, not the head": synthetic centres 1.0° apart = 2.2× noise → flagged).

## Post-M2 fixes from the user's camera-sharing test (setting OFF)

### D-023 Other-app detection: MF sensor activity monitor, not the privacy store **[verified with setting OFF]**
User report: with the setting off, (1) LookFocus kept the camera when Firefox's webcam test started, with
no release; (2) with Firefox streaming first, `debug_pose` looped "camera open (dshow) → stopped delivering
frames → reopening" every ~10 s.
- **(1) root cause, verified:** the camera privacy store only records an app once it actually streams. A
  second app blocked by LookFocus never appears there (the Python 3.14 stand-in's entry stayed at its old
  13:05 timestamp through a blocked 6 s session). The D-015 detector could never fire in the one case it
  exists for.
- **New detector:** `MFCreateSensorActivityMonitor` (mfsensorgroup.dll, Win10 1809+, the mechanism behind
  the privacy indicator), implemented in `camera_activity.py` with ctypes COM, including our own
  `IMFSensorActivitiesReportCallback` object. IIDs and vtable order come from the installed Windows SDK
  header (10.0.26100.0 `mfidl.h`). Each report lists, per device (symbolic link), every **process** with
  the device open, whether it's streaming, and controller/shared mode. Measured with the setting off:
  - LookFocus streaming, then another app opens → it shows up as **"open, not streaming"** 0.39 s later
    (with a blocked frame trickle of 5–6 frames per 6 s).
  - Firefox kept the camera **open but idle** after its webcam test stopped streaming (pid 15156 in every
    report). "Open, not streaming" therefore also means "idle holder", and the policy must handle both.
- **Policy (`camera_usage.ActivityPolicy`, pure, 13 tests):** a process that *newly* opens LookFocus's
  camera while LookFocus streams (sharing not known ON) → yield. Processes present when LookFocus got frames
  are the baseline (not blocked by it). While yielded, resume once every blocker has streamed and stopped,
  or closed, for 2 s. A blocker that never streams within 10 s was an idle holder: resume and ignore it
  until it closes the camera. Busy at start (no frames and another process *streaming*) → wait for the
  streaming ones only, with idle holders as the baseline (fixes a measured case where idle Firefox delayed
  the resume by the 10 s idle timeout).
- **Own processes:** LookFocus's PID and its sharing-probe children are excluded. The probe prints its own
  PID first, because from a venv `Popen().pid` is the launcher's, not the interpreter that opens the camera.
- **Device matching:** exact device via `device_key()` (MSMF and DSHOW paths of the same camera match),
  excluding `KSCATEGORY_SENSOR_CAMERA` links (the Windows Hello IR stream shares the device instance).
  Process names are looked up when a process is first seen (it may exit before the resume message).
- **Verified end to end, setting OFF, idle Firefox present the whole time:**
  - A (LookFocus first): the other app starts at 7.00 s → LookFocus releases at 7.89 s → the formerly
    blocked app **starts receiving frames without restarting** (222–234 frames in 8 s) → exits → LookFocus
    resumes 2 s later. The sharing probe and idle Firefox trigger nothing.
  - B (other app first): MSMF and DSHOW both rejected as "no frames" → `busy` event at 11.4 s →
    the other app exits at 15.7 s → resume at 17.7 s → open. Detection takes ~8 s because each backend gets
    its 3–4 s frame check first; acceptable for the startup-only case.
- The privacy-store reader/watcher was removed (dead code); `camera_usage.py` now holds only the pure
  policy, names and notices.
- `MEPSGFlag` reads 1 with the setting OFF too → it doesn't indicate the setting (dropped as a hint in the
  docs; still logged).

### D-024 Camera verification requires a steady stream **[verified]**
Root cause of (2): a DSHOW client blocked by another app still "opens" and trickles ~1 frame/s, and the old
check (3 frames within 3 s) accepted it. Now an open counts only after **5 real frames** (lit, or with sensor
noise; blocked clients can get all-black frames) within 3 s. A free camera does that in < 0.4 s even in a
dark room (15–30 fps). Also, "busy" now means "some device opened but gave no frames", even if other devices
failed for other reasons (the OBS virtual camera "did not open" had been masking it).

### D-025 Notification text fits the 255-character balloon **[unit-tested]**
The D-015 example was wrong: with a long app name ("Microsoft Teams") the full Settings path overflowed
the limit and fell back to the short form. The body is now "<App> started using the camera, so LookFocus
released it and will resume afterwards." plus the most specific tip that fits: path with camera name →
path with "(your camera)" → "Settings > Bluetooth & devices > Cameras" → "camera settings".

### D-026 No guessing during the first sharing probe **[verified]**
User report: with the setting ON, LookFocus released the camera for Windows Camera. The log showed probe
"on" at 16:19:03.473 and the release logged at .703 "(sharing: on)". The release decision had been taken
~0.2 s earlier (the log line comes after `release()`, which takes 0.2–0.4 s on MSMF), while the first probe
of the run was still in flight and the state was UNKNOWN, which was treated as OFF.
- While the first probe of a run is pending and the state is UNKNOWN, newcomers are **not judged**. The
  probe thread wakes the capture loop when it finishes, and they're evaluated against the real result
  (at most ~3 s later). Later reopens use the last known state while their probe runs.
- A newcomer that is **streaming** while LookFocus holds the camera is accepted outright: with the setting
  off, blocked apps always showed "open, not streaming", so a streaming one evidently isn't blocked.
- If a probe flips ON → OFF, apps accepted since the open *only* because sharing was believed on (and not
  seen streaming) become newcomers again (`ActivityPolicy.sharing_turned_off`). The flag is set by the
  probe thread and applied on the capture thread, so the policy is only ever touched by one thread.
- Verified with the setting ON: an app opening 0.8 s after LookFocus (inside the probe window) → deferred →
  probe ON → no release; both streamed (other app 240 frames). An app opening after the probe → no release
  (237 frames).
- Also from the user's run: `explorer.exe` opened the camera briefly during a Windows Camera session (maybe
  the shell's camera preview or privacy UI). It simply joins the blockers while yielded. Display names:
  WindowsCamera → "Windows Camera", explorer → "Windows Explorer".

## M3 — focus switching

### D-027 Target selection **[unit-tested; tracker wired in M4]**
- `targeting.choose_target` (pure): the window last active on the display (remembered by
  `windows_focus.ForegroundTracker`, 4 Hz `GetForegroundWindow` polling) if it still exists, is eligible and
  its centre is still on that display; else the topmost eligible window there (`EnumWindows` order); else
  nothing (do nothing).
- **Minimized windows are never targets.** They aren't "on" a display, and restoring a window the user
  minimized because they glanced at its monitor would be surprising. The ladder's restore stage still exists
  (the spec's stage 1) for targets minimized between selection and activation, and `focus_test
  --minimize-every` exercises it.
- Eligibility (`windows_focus.is_candidate`, Alt-Tab-like): visible, not DWM-cloaked (suspended UWP apps,
  other virtual desktops), not a child, not `WS_EX_NOACTIVATE`, not a tool window unless `WS_EX_APPWINDOW`,
  unowned (or `WS_EX_APPWINDOW`), not LookFocus's own process, not shell surfaces (`Progman`, `WorkerW`,
  taskbars, `Windows.UI.Core.CoreWindow` flyouts, task view / Alt-Tab hosts, `ForegroundStaging`, ...).
  **Disabled** owners stay eligible (a modal dialog is open): activation goes to
  `GetLastActivePopup(root owner)`, so the dialog gets focus.
- Window → display by the centre of the DWM extended frame bounds (`GetWindowRect` includes invisible
  resize borders on Windows 10+); the normal-position rect for minimized windows. Works for zones.
- Verified on the dev desktop: Windows Terminal, Firefox, Explorer, Electron apps, WinUI 3, Win32, UWP
  (`ApplicationFrameWindow`) and an elevated `ConsoleWindowClass` PowerShell enumerate correctly; minimized
  windows are flagged. Elevation is read from the process token (`TokenElevation`); the elevated
  PowerShell read as elevated=True from a non-elevated process.

### D-028 Activation ladder: measured on this machine **[verified with Explorer pairs; full matrix pending]**
- The ladder is pure (`targeting.activate` with an injectable `FocusApi`; 10 unit tests with a fake Windows),
  with every stage verified by polling `GetForegroundWindow` for up to 500 ms (root-owner comparison).
  Return values are recorded but never trusted; the test confirms `SetForegroundWindow` returns True
  while doing nothing.
- `AttachThreadInput` attaches the current thread to both the foreground thread and the target's, then
  `BringWindowToTop` + `SetForegroundWindow`, detaching in `finally`. The synthetic tap uses VK 0xE8
  (documented unassigned; AutoHotkey uses it as its menu-mask key for the same reason). A lone Alt tap is
  last and can be disabled (`--no-alt`).
- **Measured (two Explorer windows in zones, LookFocus as a background process, foreground lock timeout
  200000 ms):**
  - full ladder, 6 switches: plain `SetForegroundWindow` worked once (the first switch) and then failed
    5/5, each costing the full ~510 ms wait; `attach_thread_input` worked **5/5 in 0 ms**.
  - starting at `attach_thread_input` with every 3rd target minimized first, 8 switches: attach 6/6 in
    0–1 ms; restore 2/2 in 0 ms (`SW_RESTORE` activated the window by itself). Keyboard focus
    (`GetGUIThreadInfo.hwndFocus`) landed in the target in 14/14 switches.
- → Plan for M4: the daemon's focus executor **learns its first stage** (skip stages that keep failing
  while a later one works, re-probe the full ladder occasionally). That avoids a ~0.5 s delay and the
  taskbar flash a refused `SetForegroundWindow` causes. `focus_test` defaults to the full spec ladder so
  it measures every stage.
- **Elevated windows:** if the ladder fails and the target (or the current foreground window) belongs to a
  process whose token can't be read or reads elevated, while LookFocus isn't elevated, the failure is
  reported as UIPI with the `--elevated` hint. `--elevated` relaunches via `ShellExecuteW("runas")` (UAC
  prompt); there is no UIAccess. **[unverified until the user runs it]**
- `focus_test` safety: once focus moves, the terminal can't receive Ctrl+C, so the test aborts if the
  pointer moves more than 40 px or a mouse button is pressed (mouse-button state only; never the keyboard).
  Reports are also written to `%APPDATA%\LookFocus\focus_test_last.txt` (text only).
- Noted for M4: the user runs "Move Mouse" (a keep-awake jiggler). Synthetic pointer moves look like
  mouse activity to `GetCursorPos`/`GetLastInputInfo` and could hold off switching, or abort focus_test.

### D-029 focus_test findings from the user's runs, and test-harness fixes **[verified]**
User runs (24 switches each, zone mode): Windows Terminal ↔ Firefox 23/23; Terminal ↔ Explorer with
minimize every 4th 23/23 (restore 6/6); admin PowerShell ↔ Explorer 23/23 twice. Keyboard input went to
every target except the admin console (see below). No failure in 115 switches. But four test-harness
problems meant part of the matrix wasn't what it claimed:
1. **Query matching:** `--windows Code explorer` picked the *terminal running the test* for "Code", because a
   console's title holds the command line, which contains every query. Matching is now ranked: exact program
   name, exact class, then name / title / class substrings, with any window whose title contains
   `focus_test` ranked last. Checked: Code→VS Code, Settings→Settings, Administrator→admin PowerShell.
2. **The "elevated" runs were elevated both ways:** they were started from the admin PowerShell, so the test
   itself ran elevated (`this process elevated: True`). That verified **elevated LookFocus → elevated and
   normal windows: 23/23 via attach_thread_input** (what `--elevated` is for). Non-elevated LookFocus →
   elevated window is still **[unverified]**.
3. **Keyboard-focus check:** reported NO for every switch to the admin PowerShell (classic
   `ConsoleWindowClass`). Reproduced with a non-elevated `conhost` window: `GetGUIThreadInfo(window's thread)`
   returns no focus window for consoles even when they have it, while the *foreground* thread (tid 0)
   correctly reports the console as active and focused. `keyboard_focus_in` now asks about the foreground
   thread. Explorer: both methods agree.
4. **Console bias:** plain `SetForegroundWindow` worked on every switch *away from* the test's own terminal
   (to Firefox 12/12, to Explorer 6/6) and failed on every switch *back to* it. Windows grants foreground
   rights to a process attached to the foreground console. The real app has no console, so focus_test
   now runs the switching in a console-less `pythonw` child by default and streams its log (`--console`
   keeps the old mode; `--elevated` starts the child via `ShellExecuteExW("runas")` and waits on its
   handle). Console-less smoke test (Explorer ↔ Explorer): SetForegroundWindow worked only on the first
   switch, then attach_thread_input 5/5 at 0 ms, the same picture as D-028. So for the daemon, attach is
   the stage that matters.
- The user's `ForegroundLockTimeout` now reads 2147483647 ms (the lock never expires). It was 200000 earlier;
  some other software changed it. It doesn't change the picture above.

### D-030 M3 matrix complete: what each app type needs **[verified on the user's machine]**
All runs console-less (like the daemon) unless noted; 24 switches each; zone mode.

| pair | LookFocus | result | what worked |
|---|---|---|---|
| VS Code ↔ Explorer (minimize every 4th) | normal | 24/24, keyboard 24/24 | SetForegroundWindow 1/18 (first switch only); **AttachThreadInput 17/17 at 0 ms**; restore 6/6 |
| Calculator ↔ Settings (UWP) | normal | 24/24, keyboard 24/24 | **SetForegroundWindow 23/24** (0–78 ms); one switch needed the **unassigned-VK tap** after AttachThreadInput also failed |
| Calculator ↔ Settings (UWP) | elevated | 24/24 | SetForegroundWindow 24/24 |
| admin PowerShell ↔ Explorer (from an admin console) | elevated | 23/23 twice | AttachThreadInput 11/11 into the admin window |
| Terminal ↔ Firefox / Explorer (in-terminal, biased, D-029) | normal | 46/46 | — |
| **admin PowerShell ↔ Explorer** | **normal** | into admin: works (SetForegroundWindow 52 ms); **away from admin: 0/3, every stage fails** | UIPI |

- **UIPI in practice:** a non-elevated LookFocus *can* move focus **into** an elevated window but can't take
  it **away** from one. All four stages fail (~2 s wasted, plus an Alt tap aimed at an admin window,
  which Windows drops). `focus_window` now checks first: if the foreground window's process token reads
  elevated (True, not unknown) and LookFocus isn't elevated, it returns at once with the UIPI message
  (verified: immediate failure, no stages run). The spec's expectation of "a clean, clearly reported
  failure" holds for the away direction. `--elevated` (verified via the user's elevated runs) removes it.
- **No single best stage:** Win32 apps (Explorer, VS Code, Windows Terminal) need AttachThreadInput;
  UWP frames accept a plain SetForegroundWindow, and once that failed *and* AttachThreadInput failed
  while the VK tap worked. So the ladder stays; the daemon's learner (M4) picks the starting stage per
  window class instead of globally.
- No menu-bar activation or other side effects were reported; the Alt stage was never reached in a
  successful run.

## M4 — the engine

### D-031 CPU: the camera was sending 2560×1440; raw NV12 fast path **[verified]**
- First full-engine measurement (dry run, face tracked 367/367 frames, 15 fps): **10.1% of the machine**
  (80.7% of one core), over the target. Root cause: with "Allow multiple apps to use camera at the same
  time" ON, Windows serves every app the shared stream at the resolution chosen in camera settings
  (**2560×1440** here) and ignores the requested 640×480. MSMF then converts every 30 fps 3.7 MP frame
  to RGB in software (HW transforms are off): **89.6% of one core just to read**. The face tracker was
  also fed 11 MB frames (32.8 ms CPU per frame at 2560×1440 vs 6.2 ms at 640×360, no face). The earlier
  8.8% figure (D-017) was inflated the same way.
- Fix (`CameraSource._choose_mode`): for MSMF frames larger than the processing size, switch to **raw
  NV12** (`CAP_PROP_CONVERT_RGB=0`) **before the first read**; switching later keeps the RGB media type and
  only hands over raw RGB32 buffers (measured). Buffered frames are drained without copying. Only the
  processed frame is converted, straight to 640 px wide (area-averaged luma, subsampled chroma: 1.7 ms,
  vs 5.4 ms to convert full size then resize). `cap.set()` returns False even when the switch works, so
  `_verify` decides from the frames themselves, and reopens without raw mode if they aren't NV12-sized.
  Other large frames (DSHOW) are shrunk with INTER_AREA.
- Raw NV12 read at 30 fps: 24% of one core (vs 89.6%). Fast-path output vs MF's own conversion of the same
  scene: 0.9/255 mean difference, channels in the right order. Synthetic NV12 tests cover the converter.
- Engine CPU after the fix, no face in view (idle mode, ~2.6 frames/s): **1.4% of the machine**.
  **With a face tracked at 15 fps: [pending: the user wasn't at the camera]**. `lookfocus run --stats`
  prints it every 10 s.
- Consequence: **calibration is tied to the camera mode.** Turning the sharing setting off brings back the
  640×480 (4:3) mode, which may have a different field of view or crop, shifting every pose. Calibrations
  now record the native frame size; `run` and `watch` warn when it changes. Calibrations made before this
  have no size and get no warning.

### D-032 Decision state machine **[21 scripted-stream tests]**
`decision.DecisionMachine` (pure; `now` passed in) consumes one `Observation` per processed frame: the
classified display, NONE ("facing no display") or None (no face), the classifier's per-display distances,
and mouse / keyboard activity times. It returns a display id only when the faced display changes.
- Dwell 300 ms; 1 s if the keyboard was used within 2 s (typing). Mouse movement or clicks block
  switching and for 1.5 s after; the dwell starts counting only once the guard lifts, so after mouse use
  the user still needs a full dwell on the other display.
- Hysteresis (default 2.0 normalized classifier units, ≈3–4° here): a different display only counts if
  its distance beats the current display's by the margin. Entering a display from "none" or no-face isn't
  penalised beyond normal classification. Tested: poses wobbling either side of a bezel for 4 s → no
  switch.
- Gap tolerance 150 ms: one stray frame (noise, a dropped detection, a frame classified back onto the
  current display) doesn't reset a dwell in progress.
- "none" (phone, desk) and no-face keep the current display: looking back at it emits nothing (the spec's
  "only on change, never repeated").
- The current display is **seeded** from the foreground window's display at start and on resume, so
  LookFocus doesn't jump when started or unpaused; it switches when the user next looks elsewhere.
- Manual focus changes (click, Alt-Tab) don't move the decision's "current display": events follow what
  the user *faces*, so LookFocus never fights a deliberate click while the user keeps looking at the same
  display.

### D-033 Input activity without hooks **[8 tests]**
`inputactivity.InputTracker` (pure) + `InputPoller` (40 ms): `GetLastInputInfo`, `GetCursorPos`, and
`GetAsyncKeyState` for the three mouse buttons only. Keyboard activity = a new input event not explained by
the pointer moving or a button (within 60 ms). Details:
- The first poll only sets a baseline (input from before LookFocus started is unknown).
- Pointer moves under 4 px are "touched" (they explain the input event) but not "mouse use". The user's
  "Move Mouse" jiggler therefore neither blocks switching nor reads as typing.
- LookFocus's own injected key taps are excluded with an ignore window around each activation, and its own
  pointer move (`--move-cursor`) resets the baseline.
- Known blind spot: wheel scrolling moves no pointer, so it reads as typing (it only lengthens the dwell).

### D-034 Stage learner **[6 tests]**
`targeting.StageLearner`, keyed by window class: start the ladder at the earliest stage that won among the
last 6 successes for that class; the full ladder with no history and on every 25th switch. This follows
D-030: Win32 classes learn `attach_thread_input`, which avoids the ~0.5 s refused-SetForegroundWindow wait
and taskbar flash; UWP frames keep `set_foreground`. Restore (minimized targets) always runs.

### D-035 Engine wiring **[verified in dry runs]**
`daemon.Daemon` threads: capture → LatestSlot → decision loop → one-slot FocusExecutor (a newer request
replaces one that hasn't started); input poller; 4 Hz foreground tracker; display layout re-checked every
5 s (a mismatch with the calibration pauses switching and emits `layout_changed`).
- Only *fresh* frames are classified. A 100 ms wait timeout with no new frame is a gap, so a stalled camera
  can't complete a dwell on a stale pose.
- Target selection excludes topmost, caption-less, non-taskbar pop-ups: a dry run picked "Malwarebytes Tray
  Notification" as the window to focus.
- `lookfocus run [--dry-run] [--stats] [--move-cursor] [--no-alt] [--elevated]` runs it in a console with
  the tuning flags. The tray, config file, hotkey and lock/sleep handling wrap the same Daemon in M5.
- Dry run with the user present (before the fast path): adopted Zone 1 silently at start, then one switch
  decision when the user turned to Zone 2; camera-sharing probe ON.

M4 hardware result (user, `lookfocus run --stats`, zones, face tracked 100%): **CPU 6.1–7.8% of the machine at
15 fps** (49–63% of one core; inference 16–20 ms). Learned stages: Windows Terminal via attach_thread_input
in 0–1 ms after the first (full-ladder) switch; Firefox via set_foreground, since `run` is a console process
(console bias, D-029). The tray app runs console-less, so its learner will see the daemon-faithful picture.
"Everything worked well."

## M5 — tray, hotkey, autostart, lock/sleep, config, logs

### D-036 Tray app **[scripted end-to-end run: 14/14 checks; real launch verified]**
`tray.TrayApp`: pystray icon (main thread) around the M4 `Daemon`. States (drawn icons, no assets):
**active** (green eye), **paused** (grey bars), **no-face** (amber ring; also "camera opening"),
**calibrating** (blue crosshair), **error** (red "!": not calibrated, one display without zones, layout
changed). Camera released for another app shows as paused with the reason in the tooltip.
- Menu: live status line, **Pause/Resume** (also the left-click default), **Calibrate / Recalibrate** (+
  "Cancel calibration" while it runs), **Choose camera** (Automatic + live MSMF list, stored as device path +
  name), **Camera sharing…** (the user-requested setup step as a dialog: ON / OFF with "open camera
  settings?" / not available), **Open config**, **Debug view**, **Start with Windows** (checkmark), **Quit**.
- pystray's Windows backend shows a menu built at the last `update_menu()`. A small subclass rebuilds it on
  every right-click so the status line and camera list are current (relies on pystray 0.19.5 internals;
  the version is pinned).
- Notifications (tray balloons, can be turned off with `"notifications": false`): camera released for /
  busy because of another app (D-015/D-023 text), display layout changed, camera resolution changed
  (D-031), the admin-window limitation (once per run), config problems, calibration results, hotkey
  pause/resume.
- The camera-sharing dialog is also offered **once**, automatically, the first time a probe reads OFF on a
  build that has the setting (remembered in `%APPDATA%\LookFocus\state.json`).
- Recalibrate from the tray stops the engine (releasing the camera), runs the overlay on its own thread, and
  brings the overlay to the foreground through the activation ladder so Esc reaches it (there's no terminal
  for Ctrl+C). Then it restarts the engine on the new calibration.
- Debug view in the tray shows the **running engine's** frames through the capture debug hook (no second
  camera client), plus decision state and the faced display. The viewer moved into the package
  (`lookfocus/debugview.py`); `tools/debug_pose.py` is now a thin wrapper with its own camera.
- Single instance via a named mutex; a second launch shows "already running".

### D-037 Pause reasons, lock, sleep, display power **[verified: events + scripted pause logic]**
Pausing is a *set of reasons*, `user`, `lock`, `sleep`, `display`: paused while any is set, so unlocking
never overrides a manual pause (scripted check: manual resume while locked stays paused until unlock).
Paused = no switching **and the camera released** (LED off).
- `sysevents.SystemEvents`: a hidden **top-level** window (WM_POWERBROADCAST doesn't reach message-only
  windows) on its own thread: `WTSRegisterSessionNotification` lock/unlock; `PBT_APMSUSPEND` /
  `PBT_APMRESUME*`; and `GUID_CONSOLE_DISPLAY_STATE` via `RegisterPowerSettingNotification`, because Modern
  Standby laptops like this one may never send classic suspend. Display off → pause, on → resume; this
  also covers the screen timing out while the user is away. Windows delivers the current display state on
  registration. Verified: it read **off** while the screen had timed out on battery (idle 256 s > the 180 s
  DC display timeout), and **on** when the user was back.
- `WM_DISPLAYCHANGE`: if the engine isn't running (for example because a monitor was missing), try again
  2 s later. The engine itself re-checks the layout every 5 s (D-035).

### D-038 Config file **[20 tests; live reload verified in the scripted run]**
`%APPDATA%\LookFocus\config.json`, written with defaults on first run: `dwell_ms`, `typing_dwell_ms`,
`typing_grace_ms`, `mouse_grace_ms`, `hysteresis`, `none_margin`, `move_cursor_on_switch`,
`allow_alt_fallback`, `fps`, `camera_backend`, `camera_index` / `camera_name` / `camera_path`,
`yield_camera_to_other_apps`, `zones`, `pose_source`, `hotkey`, `notifications`, `log_level`.
- Each field is type- and range-checked; a bad value falls back to its default **individually** and is
  reported (tray notification + log); unknown keys are reported (typos); unreadable JSON → defaults.
- Live reload: a watcher checks the file every 2 s. `hotkey`, `log_level` and `notifications` apply in
  place; anything else restarts the engine (about 1 s). "Choose camera" writes the config the same way.
- First run seeds `zones` from an existing calibration, and `lookfocus calibrate --zones N` writes `zones`
  back, so the tray and the calibration agree (otherwise one monitor + zones 0 = "only one display").
- `lookfocus run` now takes its defaults from the config; command-line flags override.

### D-039 Hotkey, autostart, logs, elevation **[verified]**
- Pause hotkey via `RegisterHotKey` with `MOD_NOREPEAT` on the events window (no hook). Default
  `ctrl+alt+shift+g`; parsed from the config (modifiers + letter/digit/F1–F24/named key; at least one
  modifier required). If another app already owns it (error 1409), a notification says so.
- Start with Windows: `HKCU\...\Run\LookFocus = "<venv>\Scripts\pythonw.exe" -m lookfocus tray` (no console;
  the frozen build will use its exe). Plain `lookfocus` also starts the tray. Verified: a real `pythonw`
  launch created the config, registered the hotkey, started the engine on the NV12 fast path, ran the
  sharing probe.
- Logs: `%APPDATA%\LookFocus\logs\lookfocus.log`, rotating 1 MB × 3. Text only: never images, never
  keystrokes. Window titles appear truncated to 40 characters (e.g. "focused 'Inbox - …' via …"); local
  only, but noted in the README.
- `lookfocus tray --elevated` relaunches as administrator (UAC prompt) so it can take focus *from*
  administrator windows (D-030). The Run key can't elevate silently, so autostart is always non-elevated,
  as documented. No UIAccess.

M5 hardware result (user): tray, hotkey, lock/unlock, sleep and display off, autostart, tray recalibration,
debug view, camera menus, live config edits and quit all checked: "Everything looks great!"

## M6 — hardening, README, optional standalone build

### D-040 Hardening: failures are logged, contained and visible **[5 tests + soak run]**
A review of what could fail silently under `pythonw` (no console, `sys.stderr` is None):
- **Crash logging:** the tray installs `sys.excepthook` and `threading.excepthook`, so an uncaught
  exception in any thread reaches the log file instead of vanishing.
- **Loops that must not die:** the decision loop and the input poller now catch and log per iteration
  (throttled: 1st, 10th, then every 1000th repeat). A dead input thread would have silently disabled the
  mouse and typing guards; a dead decision loop would have ended switching for the session. The
  foreground tracker and focus executor already did; the capture loop guards inference, and anything else
  that ends it is caught by the watchdog below.
- **Engine watchdog:** every 2 s the tray checks `Daemon.dead_threads()`. If any engine thread has ended, it
  restarts the engine, at most 3 times in 10 min. After that it stays in the red error state
  ("stopped: <reason>", with a notification pointing to the log), so a persistent fault such as the privacy
  check never loops. An exception while *starting* the engine also shows as an error instead of being lost
  in pystray's setup thread.
- **Secure desktop:** `GetCursorPos` fails while a UAC prompt or the lock screen is up. It used to return
  (0, 0), which read as a big mouse movement; it now returns the last known position.
- **Autostart self-heal:** if Start with Windows is on but the Run key points at another copy (the venv
  moved, or the user switched to the standalone build), the tray rewrites it at startup.
- **Relaunch commands:** `paths.app_command()` builds "run this LookFocus again" for both source and frozen
  runs (pythonw/python -m lookfocus, or LookFocus.exe/lookfocus-cli.exe). It's used by autostart,
  `tray --elevated` and `run --elevated`, which used to hardcode `-m lookfocus`.
- Already handled earlier, and reviewed: the camera's disconnect and stall reopen with backoff (D-014), a
  bad config falls back field by field (D-038), an unreadable calibration shows "not calibrated", and
  pystray re-adds the icon when Explorer restarts (`TaskbarCreated`).

**Soak test** (real engine in dry run, real camera, 8.7 min): 4.5 min steady, then 10 pause/resume cycles,
then 5 full engine restarts.
- No dead threads. Every status kind was reported; 16 sharing probes completed.
- Python threads 8 throughout and 1 after the final stop.
- Private memory +2.6 MB over the run; GDI 0; USER objects 14 → 16, and 9 after stopping.
- Kernel handles were flat while steady, then +2.3 per pause cycle and +23 per engine restart; back to the
  starting count after the final stop.

The per-component breakdown is in D-042.

### D-041 Memory: OpenBLAS limited to one thread **[measured]**
The soak showed **490 MB private** memory. Stage by stage, `import numpy` alone committed **235 MB**:
numpy's bundled OpenBLAS reserves a buffer per core and starts a thread per core (8 here), although almost
none of it becomes resident (working set +12 MB). LookFocus only does tiny matrix maths (affine fits over
5 points), so `lookfocus/__init__.py` now sets `OPENBLAS_NUM_THREADS=1` (setdefault, so it can be
overridden), next to the MSMF variable and for the same reason: it must happen before numpy loads.
- Result, with the capture running: private **454 → 228 MB**, working set unchanged (~193 MB), 7 fewer idle
  threads.
- Standalone tray: 267 MB private, 209 MB working set.

### D-042 Handle growth per camera reopen is inside OpenCV/Media Foundation **[measured]**
Handle growth over 10 cycles of each component:
- FaceTracker create/close: 0
- SensorActivityMonitor start/stop: 0
- `CameraSource` open/read/release: +2.5 handles and +0.4 USER objects per cycle
- CaptureThread start/stop: +1.4
- sharing probe: +1.3

A bare `cv2.VideoCapture(0, CAP_MSMF)` open/read/release loop grows by **+5 handles per reopen, linearly**
over 40 reopens; USER objects stop growing after the first batch. So the growth comes from OpenCV's MSMF
backend or Media Foundation, not from LookFocus, and can't be fixed here except by reopening less.
LookFocus reopens only on pause/resume, lock/unlock, display off/on, yielding the camera, and engine
restarts: a few dozen times a day, so tens of thousands of days before the per-process handle limit
matters. Recorded and not worked around.

### D-043 Optional standalone build: PyInstaller one-folder, two executables **[built, self-checked, audited]**
`tools/build_exe.py` + `packaging/lookfocus.spec`, PyInstaller **6.22.3** (pinned in
`requirements-build.lock`; build-time only and not in the runtime lock). Output `dist\LookFocus\`:
- **`LookFocus.exe`** (no console): the tray, which is also what autostart points at. **`lookfocus-cli.exe`**
  (console): `calibrate`, `run`, `watch`, `camera-sharing`. Both share one `_internal` folder. A
  windowed-only build would leave the CLI with no output and no stdin for calibration prompts.
- `packaging/entry.py` dispatches `--probe-child`. In the frozen app, the camera-sharing probe re-runs the
  app's own exe, since there's no `python -m`. Verified: the probe child ran and read ON.
- mediapipe loads `tasks/c/libmediapipe.dll` via ctypes + `importlib.resources`, which PyInstaller can't
  see. It's collected with `collect_dynamic_libs("mediapipe")` at its package path, where
  `privacy.verify_runtime()` checks it.
- **OpenCV's FFmpeg plugin is left out** (`opencv_videoio_ffmpeg*.dll`, 29 MB, links ws2_32). It only
  serves video files and network streams; camera capture via MSMF/DSHOW is in `cv2.pyd`. `cv2.pyd`
  itself imports `wsock32.dll` and has to stay. The dynamic audits have never seen it connect.
- **matplotlib can't be left out:** the first build excluded it, and the frozen engine then reported "face
  tracker unavailable: No module named 'matplotlib'". mediapipe's `vision/__init__` imports its drawing
  utils, which import `matplotlib.pyplot`. The watchdog path did its job (error reported, no crash), but the
  build's `--help` smoke test had passed, so the build now also runs **`lookfocus-cli.exe --self-check`**:
  privacy checks, model, mediapipe and one inference inside the bundle. Side effect: PyInstaller's
  matplotlib runtime hook gives each run a fresh temp `MPLCONFIGDIR` (font list rebuilt in ~0.4 s, removed
  at exit; a force-killed run leaves ~100 KB behind).
- Checks in `build_exe.py`: both executables present; model SHA-256; mediapipe native libraries
  network-clean; no FFmpeg plugin; `--help`; `--self-check`. Result: **224 MB**, OK.
- **Verified on this machine:**
  - `lookfocus-cli.exe -v run --dry-run --stats` for 60 s under `tools/net_audit.py --pid` (new mode:
    polls the TCP and UDP tables for the process and every descendant): camera on the NV12 fast path,
    sharing probe ON, **no TCP connections or UDP sockets** from it or its 3 child processes; ~0.9% CPU
    with no face in view.
  - `LookFocus.exe`: hotkey registered, engine started, camera open, probe ON; a second launch shows
    "already running".
  - Positive control for the audit mode: a parent whose grandchild binds UDP and opens a loopback TCP
    connection; all 4 endpoints reported.
- Unsigned, and PyInstaller executables are a known antivirus false-positive pattern. The README says so,
  explains SmartScreen's "Run anyway", and recommends building from source. No installer (out of scope).

### D-044 README **[written]**
The README is the user guide:
- install from source;
- calibration in multi-monitor and zone mode;
- the tray (icon states, menu, hotkey, automatic pauses) and when switching does and doesn't happen;
- camera placement and lighting (fixed camera, centred, displays ≥ ~10° apart, distance, backlight,
  shutter, one face);
- a symptom → setting tuning table plus a full config reference with defaults and ranges;
- video calls and the camera-sharing setting;
- administrator windows, troubleshooting and uninstall;
- known limitations, with the untested multi-monitor items listed explicitly: different DPI per monitor,
  different resolutions, vertical, offset and portrait arrangements, hot-plugging and docking, external and
  DirectShow-only cameras, Windows 10, and exclusive full-screen apps.

Version set to 1.0.0.
