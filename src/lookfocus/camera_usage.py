"""Which other apps are using a camera right now, and when LookFocus should step aside.

Windows records camera use per app in the camera privacy store (the source of the taskbar's "camera in
use" indicator):
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\webcam
        <PackageFamilyName>                 packaged apps (e.g. MSTeams_8wekyb3d8bbwe)
        NonPackaged\\<exe path, '\\' -> '#'>   desktop apps
each with LastUsedTimeStart / LastUsedTimeStop FILETIMEs; Stop == 0 means "in use now". Entries are per
app, not per camera device, and every process of one executable shares an entry.

The pure parts (parsing, diffing, YieldPolicy, notice text) have no Win32 imports and are unit tested;
UsageWatcher is the Windows-only part (RegNotifyChangeKeyValue, with a slow rescan as a fallback).
"""

from __future__ import annotations

import functools
import logging
import re
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

CONSENT_WEBCAM = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"
NON_PACKAGED = "NonPackaged"

# Title <= 63 and body <= 255 characters: the limits of a Windows tray balloon (NOTIFYICONDATAW).
NOTICE_TITLE_MAX, NOTICE_BODY_MAX = 63, 255
SETTING_NAME = "Allow multiple apps to use camera at the same time"


@dataclass(frozen=True)
class CameraUse:
    app_id: str  # "NonPackaged\\C:#...#Teams.exe" or a package family name
    start: int  # FILETIME of LastUsedTimeStart (0 if never)
    stop: int  # FILETIME of LastUsedTimeStop (0 while in use)

    @property
    def in_use(self) -> bool:
        return self.start > 0 and self.stop == 0

    @property
    def display_name(self) -> str:
        return app_display_name(self.app_id)


def app_display_name(app_id: str) -> str:
    """Human name: exe base name for desktop apps, a tidied package name for packaged ones."""
    if app_id.startswith(NON_PACKAGED + "\\"):
        exe = app_id.rsplit("#", 1)[-1]
        return re.sub(r"\.exe$", "", exe, flags=re.I)
    name = app_id.split("_", 1)[0]  # drop the publisher hash
    name = re.sub(r"^(Microsoft|MicrosoftWindows|Windows)\.", "", name, flags=re.I)
    known = {
        "WindowsCamera": "Camera",
        "immersivecontrolpanel": "Settings",
        "MSTeams": "Microsoft Teams",
        "XboxGamingOverlay": "Xbox Game Bar",
    }
    return known.get(name, name)


def exe_app_id(exe_path: str) -> str:
    """ConsentStore key for a desktop executable."""
    return NON_PACKAGED + "\\" + exe_path.replace("\\", "#")


def started_since(
    prev: dict[str, CameraUse], cur: dict[str, CameraUse], exclude: set[str]
) -> list[CameraUse]:
    """Apps that began a camera session since `prev` (new in-use entry, or a new start time)."""
    excl = {e.casefold() for e in exclude}
    out = []
    for app_id, use in cur.items():
        if app_id.casefold() in excl or not use.in_use:
            continue
        before = prev.get(app_id)
        if before is None or not before.in_use or before.start != use.start:
            out.append(use)
    return sorted(out, key=lambda u: u.start)


def in_use_now(cur: dict[str, CameraUse], exclude: set[str]) -> set[str]:
    excl = {e.casefold() for e in exclude}
    return {a for a, u in cur.items() if u.in_use and a.casefold() not in excl}


def yield_notice(apps: list[str], sharing_available: bool, camera: str = "") -> tuple[str, str]:
    """(title, body) telling the user LookFocus let another app have the camera."""
    who = _join(apps)
    title = f"Camera released for {who}"
    body = f"{who} started using the camera, so LookFocus released it. It resumes when {who} is done."
    if sharing_available:
        cam = f" > {camera}" if camera and len(camera) <= 24 else ""
        tip = (
            f" To use both at once, turn on '{SETTING_NAME}' in Settings > Bluetooth & devices"
            f" > Cameras{cam} > Advanced camera options."
        )
        if len(body + tip) <= NOTICE_BODY_MAX:
            body += tip
        else:
            body += f" To use both at once, turn on '{SETTING_NAME}' in camera settings."
    if len(title) > NOTICE_TITLE_MAX:
        title = "Camera released for another app"
    return title, body[:NOTICE_BODY_MAX]


def busy_notice(apps: list[str], sharing_available: bool) -> tuple[str, str]:
    """(title, body) when LookFocus starts while another app already holds the camera."""
    who = _join(apps)
    title = (
        "Camera in use" if len(f"Camera in use by {who}") > NOTICE_TITLE_MAX else f"Camera in use by {who}"
    )
    body = f"{who} is using the camera, so LookFocus is waiting and starts when {who} is done."
    tip = f" To use both at once, turn on '{SETTING_NAME}' in camera settings."
    if sharing_available and len(body + tip) <= NOTICE_BODY_MAX:
        body += tip
    return title, body[:NOTICE_BODY_MAX]


def _join(apps: list[str]) -> str:
    if not apps:
        return "Another app"
    return apps[0] if len(apps) == 1 else ", ".join(apps[:-1]) + " and " + apps[-1]


@dataclass
class YieldPolicy:
    """Decides when LookFocus should release the camera for another app, and when to take it back.

    - While holding: another app *starting* a session triggers a yield, unless camera sharing is known
      to be on (then both can stream). Apps already in use when LookFocus got frames don't trigger one:
      LookFocus getting frames at all means they aren't blocking each other.
    - While yielded: resume once none of the apps that caused the yield (or any app that started since)
      has been in use for `grace_s`.
    - If the user resumes by hand while those apps still hold the camera, they are ignored until their
      current session ends, so the policy doesn't immediately yield again.
    """

    grace_s: float = 2.0
    yielded: bool = False
    blockers: set[str] = field(default_factory=set)
    ignored: dict[str, int] = field(default_factory=dict)  # app_id -> session start that was waived
    _clear_since: float | None = None

    def on_usage(
        self, now: float, started: list[CameraUse], current: dict[str, CameraUse], sharing_on: bool
    ) -> str | None:
        """Returns "yield", "resume" or None."""
        self._expire_ignored(current)
        fresh = [u for u in started if self.ignored.get(u.app_id) != u.start]
        if not self.yielded:
            if fresh and not sharing_on:
                self.yielded = True
                self.blockers = {u.app_id for u in fresh}
                self._clear_since = None
                return "yield"
            return None
        self.blockers |= {u.app_id for u in fresh}
        busy = {a for a in self.blockers if a in current and current[a].in_use}
        if busy:
            self._clear_since = None
            return None
        if self._clear_since is None:
            self._clear_since = now
        if now - self._clear_since >= self.grace_s:
            self.yielded = False
            self.blockers = set()
            self._clear_since = None
            return "resume"
        return None

    def block_on(self, app_ids: set[str]) -> bool:
        """LookFocus got no frames while these apps use a camera: wait for them as if it had yielded."""
        apps = {a for a in app_ids if a not in self.ignored}
        if self.yielded or not apps:
            return False
        self.yielded = True
        self.blockers = apps
        self._clear_since = None
        return True

    def user_resume(self, current: dict[str, CameraUse]) -> None:
        for a in self.blockers:
            if a in current and current[a].in_use:
                self.ignored[a] = current[a].start
        self.yielded = False
        self.blockers = set()
        self._clear_since = None

    def _expire_ignored(self, current: dict[str, CameraUse]) -> None:
        for a, start in list(self.ignored.items()):
            u = current.get(a)
            if u is None or not u.in_use or u.start != start:
                del self.ignored[a]


# ------------------------------------------------------------------------------------------------
# Windows-only: reading the store and watching it for changes
# ------------------------------------------------------------------------------------------------


def read_usage() -> dict[str, CameraUse]:
    """Snapshot of every app entry in the current user's camera privacy store."""
    import winreg

    out: dict[str, CameraUse] = {}

    def entry(key, app_id: str) -> None:
        try:
            start = int(winreg.QueryValueEx(key, "LastUsedTimeStart")[0])
            stop = int(winreg.QueryValueEx(key, "LastUsedTimeStop")[0])
        except OSError:
            return
        out[app_id] = CameraUse(app_id, start, stop)

    def children(key):
        i = 0
        while True:
            try:
                yield winreg.EnumKey(key, i)
            except OSError:
                return
            i += 1

    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, CONSENT_WEBCAM)
    except OSError:
        return out
    with root:
        for name in list(children(root)):
            try:
                sub = winreg.OpenKey(root, name)
            except OSError:
                continue
            with sub:
                if name == NON_PACKAGED:
                    for exe in list(children(sub)):
                        try:
                            with winreg.OpenKey(sub, exe) as k:
                                entry(k, NON_PACKAGED + "\\" + exe)
                        except OSError:
                            continue
                else:
                    entry(sub, name)
    return out


@functools.cache
def own_app_ids() -> frozenset[str]:
    """ConsentStore ids that are LookFocus itself (the running executable, and the venv's base python)."""
    import ctypes

    ids = set()
    buf = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf)):
        ids.add(exe_app_id(buf.value))
    for p in (sys.executable, getattr(sys, "_base_executable", "")):
        if p:
            ids.add(exe_app_id(p))
    return frozenset(ids)


UsageCallback = Callable[[list[CameraUse], dict[str, CameraUse]], None]


class UsageWatcher(threading.Thread):
    """Calls `callback(started, current)` whenever another app starts (or stops) using a camera.

    Woken by RegNotifyChangeKeyValue on the ConsentStore key (whole subtree); also rescans every
    `rescan_s` in case a notification is missed. No polling loop beyond that.
    """

    def __init__(self, callback: UsageCallback, *, rescan_s: float = 5.0, exclude: set[str] | None = None):
        super().__init__(name="camera-usage", daemon=True)
        self.callback = callback
        self.rescan_s = rescan_s
        self.exclude = self.default_exclude() if exclude is None else exclude
        self._stop_evt = threading.Event()
        self._k32 = None
        self._stop_handle = None

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_evt.set()
        if self._k32 is not None and self._stop_handle:
            self._k32.SetEvent(self._stop_handle)
        self.join(timeout)

    @staticmethod
    def default_exclude() -> set[str]:
        return set(own_app_ids())

    def run(self) -> None:
        import ctypes
        import winreg
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k32.SetEvent.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.WaitForMultipleObjects.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        k32.WaitForMultipleObjects.restype = wintypes.DWORD
        adv.RegNotifyChangeKeyValue.argtypes = [
            wintypes.HKEY,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.HANDLE,
            wintypes.BOOL,
        ]
        adv.RegNotifyChangeKeyValue.restype = wintypes.LONG
        REG_NOTIFY_CHANGE_NAME, REG_NOTIFY_CHANGE_LAST_SET = 0x1, 0x4
        WAIT_OBJECT_0 = 0

        change = k32.CreateEventW(None, False, False, None)
        self._stop_handle = k32.CreateEventW(None, True, False, None)
        self._k32 = k32
        if self._stop_evt.is_set():
            k32.SetEvent(self._stop_handle)
        handles = (wintypes.HANDLE * 2)(change, self._stop_handle)
        key = None
        try:
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, CONSENT_WEBCAM, 0, winreg.KEY_NOTIFY | winreg.KEY_READ
                )
            except OSError as e:
                log.warning("camera privacy store not readable (%s); other-app detection off", e)
                return
            prev = read_usage()
            while not self._stop_evt.is_set():
                rc = adv.RegNotifyChangeKeyValue(
                    key.handle, True, REG_NOTIFY_CHANGE_NAME | REG_NOTIFY_CHANGE_LAST_SET, change, True
                )
                if rc != 0:
                    log.warning(
                        "RegNotifyChangeKeyValue failed (%d); falling back to %.0fs rescans",
                        rc,
                        self.rescan_s,
                    )
                w = k32.WaitForMultipleObjects(2, handles, False, int(self.rescan_s * 1000))
                if w == WAIT_OBJECT_0 + 1 or self._stop_evt.is_set():
                    break
                cur = read_usage()
                started = started_since(prev, cur, self.exclude)
                changed = started or {a for a, u in cur.items() if prev.get(a) != u}
                prev = cur
                if changed:
                    try:
                        self.callback(started, cur)
                    except Exception:
                        log.exception("camera usage callback failed")
        finally:
            if key is not None:
                key.Close()
            k32.CloseHandle(change)
            k32.CloseHandle(self._stop_handle)
