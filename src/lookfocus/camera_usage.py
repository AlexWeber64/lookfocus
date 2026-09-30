"""When LookFocus should step aside for another app that wants the camera, and what to tell the user.

Input is what the MF sensor activity monitor (camera_activity.py) reports for LookFocus's camera device:
the *other* processes that have it open, and whether each is streaming. With Windows' multi-app setting
off, a second app that opens the camera while LookFocus streams shows up as "open, not streaming" and
gets no frames (measured, D-023).

"Open, not streaming" is ambiguous: a blocked app that wants frames looks the same as an app idly keeping
the device open (Firefox keeps it open after a webcam page stops its stream, for example). ActivityPolicy
resolves that with timing:
- a process that *newly opens* the camera while LookFocus streams (and sharing isn't known to be on) makes
  LookFocus release the camera ("yield");
- processes already there when LookFocus got frames aren't blocked by it and are ignored;
- after yielding, LookFocus resumes once every blocking process has streamed and stopped, or closed the
  camera, for `grace_s`; one that never starts streaming within `idle_timeout_s` was only holding the device
  and is ignored from then on.

Pure logic (no Win32 imports); unit tested with scripted snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Title <= 63 and body <= 255 characters: the limits of a Windows tray balloon (NOTIFYICONDATAW).
NOTICE_TITLE_MAX, NOTICE_BODY_MAX = 63, 255
SETTING_NAME = "Allow multiple apps to use camera at the same time"

_FRIENDLY = {
    "firefox": "Firefox",
    "chrome": "Google Chrome",
    "msedge": "Microsoft Edge",
    "msedgewebview2": "Microsoft Edge WebView",
    "brave": "Brave",
    "opera": "Opera",
    "ms-teams": "Microsoft Teams",
    "teams": "Microsoft Teams",
    "zoom": "Zoom",
    "slack": "Slack",
    "discord": "Discord",
    "skype": "Skype",
    "webex": "Webex",
    "obs64": "OBS Studio",
    "windowscamera": "Camera",
    "systemsettings": "Settings",
}


def friendly_app_name(exe: str) -> str:
    """'firefox' -> 'Firefox'; unknown executables keep their own name; '' -> 'Another app'."""
    base = exe.rsplit("\\", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return _FRIENDLY.get(base.lower(), base) or "Another app"


Others = dict[int, bool]  # pid -> streaming, for processes other than LookFocus on LookFocus's camera


@dataclass
class ActivityPolicy:
    grace_s: float = 2.0
    idle_timeout_s: float = 10.0
    yielded: bool = False
    blockers: set[int] = field(default_factory=set)
    baseline: set[int] = field(default_factory=set)  # present when LookFocus got frames: not blocked by it
    ignored: set[int] = field(default_factory=set)  # idle holders / waived by the user
    _joined: dict[int, float] = field(default_factory=dict)  # blocker pid -> when it joined
    _streamed: set[int] = field(default_factory=set)
    _clear_since: float | None = None

    def start_holding(self, others: Others) -> None:
        """LookFocus just got frames: whoever else has the camera open isn't being blocked by it."""
        self.baseline = set(others)

    def update(self, now: float, others: Others, holding: bool, sharing_on: bool) -> str | None:
        """Returns "yield", "resume" or None."""
        present = set(others)
        self.baseline &= present
        self.ignored &= present
        if not self.yielded:
            if not holding:
                return None
            newcomers = present - self.baseline - self.ignored
            if sharing_on:  # both can stream; nobody to make room for
                self.baseline |= newcomers
                return None
            if newcomers:
                self._enter(now, newcomers)
                return "yield"
            return None

        for p in present - self.baseline - self.ignored - self.blockers:
            self.blockers.add(p)
            self._joined[p] = now
        live = {p for p in self.blockers if p in present}
        self._streamed |= {p for p in live if others[p]}
        streaming = {p for p in live if others[p]}
        still_starting = {
            p for p in live - self._streamed if now - self._joined.get(p, now) < self.idle_timeout_s
        }
        if streaming or still_starting:
            self._clear_since = None
            return None
        if self._clear_since is None:
            self._clear_since = now
        if now - self._clear_since < self.grace_s:
            return None
        self.ignored |= live  # still open but done (or never streamed): don't let them re-trigger a yield
        self._reset()
        return "resume"

    def block_on(self, now: float, others: Others) -> bool:
        """LookFocus got no frames: if other processes are streaming from the camera, wait for them."""
        busy = {p for p, streaming in others.items() if streaming} - self.ignored
        if self.yielded or not busy:
            return False
        self.baseline = set(others) - busy  # idle holders (e.g. Firefox keeping the device open) aren't it
        self._enter(now, busy)
        self._streamed = set(busy)
        return True

    def user_resume(self, others: Others) -> None:
        """The user took the camera back: waive the current blockers until they close the camera."""
        self.ignored |= {p for p in self.blockers if p in others}
        self._reset()

    def _enter(self, now: float, pids: set[int]) -> None:
        self.yielded = True
        self.blockers = set(pids)
        self._joined = dict.fromkeys(pids, now)
        self._streamed = set()
        self._clear_since = None

    def _reset(self) -> None:
        self.yielded = False
        self.blockers = set()
        self._joined = {}
        self._streamed = set()
        self._clear_since = None


def _join(apps: list[str]) -> str:
    apps = list(dict.fromkeys(apps))  # dedupe, keep order
    if not apps:
        return "Another app"
    return apps[0] if len(apps) == 1 else ", ".join(apps[:-1]) + " and " + apps[-1]


def yield_notice(apps: list[str], sharing_available: bool, camera: str = "") -> tuple[str, str]:
    """(title, body) telling the user LookFocus let another app have the camera."""
    who = _join(apps)
    title = f"Camera released for {who}"
    body = f"{who} started using the camera, so LookFocus released it and will resume afterwards."
    if sharing_available:
        # Most specific tip that fits the balloon's 255 characters.
        start = f" To use both at once, turn on '{SETTING_NAME}' in"
        tips = [
            f"{start} Settings > Bluetooth & devices > Cameras > {camera} > Advanced camera options."
            if camera
            else "",
            f"{start} Settings > Bluetooth & devices > Cameras > (your camera) > Advanced camera options.",
            f"{start} Settings > Bluetooth & devices > Cameras.",
            f"{start} camera settings.",
        ]
        body += next((t for t in tips if t and len(body + t) <= NOTICE_BODY_MAX), tips[-1])
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
