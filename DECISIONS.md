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
