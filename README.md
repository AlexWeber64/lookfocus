<h1 align="center">
  <img src="docs/media/icon-active.png" width="48" alt=""><br>
  LookFocus
</h1>

<p align="center"><b>Look at a monitor and just type.</b></p>

<p align="center">
LookFocus notices which screen you're facing and puts your keyboard there. No reaching for the mouse,
no clicking a window first. It runs quietly in the Windows tray and never goes online.
</p>

<p align="center">
  <a href="docs/media/lookfocus-demo.mp4">
    <img src="docs/media/lookfocus-demo.gif" width="800" alt="Two monitors: the user turns their head from the left screen to the right one and keyboard focus follows">
  </a>
  <br>
  <sub><a href="docs/media/lookfocus-demo.mp4">▶ Watch the 21-second video (with sound)</a></sub>
</p>

---

## The problem

With two screens, typing on the "other" one always costs an extra move:

| Without LookFocus | With LookFocus |
|---|---|
| Look at the other screen | Look at the other screen |
| Find the mouse, move it across | |
| Click the window, hope it was the right spot | |
| Type | Type |

Your eyes already made the decision. LookFocus just acts on it.

## How it works

1. **Show it your screens, once.** A dot appears on each screen, you look at it, done. It takes about a
   minute.
2. **Turn your head.** Your webcam sees which way your head points. Hold it there for a moment (about a third of
   a second) and LookFocus knows which screen you mean.
3. **Type.** The window you last used on that screen already has the keyboard.

That's the whole thing. It follows your **head**, not your eyes, so quick glances don't count.

## What makes it pleasant to live with

**It remembers every screen.** Each screen keeps its own "last window": your editor on the left, your notes on
the right. Turning back brings you back to exactly where you were typing.

**It isn't twitchy.**
- A quick glance doesn't switch anything. You have to actually look.
- While you're typing, it waits a little longer, so focus never jumps in the middle of a word.
- While you're using the mouse, it stays out of the way. A click is already a decision.
- Looking down at your phone or the desk? Nothing happens.
- If you click or Alt-Tab yourself, it never undoes it.

**It gets out of the way.**
- Pause any time with **Ctrl+Alt+Shift+G**, or by clicking the tray icon.
- It pauses by itself (and turns the camera off) when you lock the PC, it sleeps, or the screen turns off.
- On a video call it hands the camera over and picks up again afterwards, or both run side by side if Windows
  allows (see the FAQ).

**It's private.**
- Camera images stay in memory: never saved, never sent anywhere. Each frame becomes three head angles and is
  thrown away.
- It never connects to the internet. There's no account and no telemetry.
- No keylogging: it notices *that* you typed, never *what*.

**One monitor? Split it.** On a single wide screen, LookFocus can treat the left and right halves (or up to
eight strips) as separate "screens".

## Get started

You'll need Windows 11, a webcam in front of you (the built-in laptop camera is fine) and
[Python 3.12](https://www.python.org/downloads/). In PowerShell, inside this folder:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
```

**Two or more monitors:**
```powershell
.venv\Scripts\pythonw.exe -m lookfocus
```
A tray icon appears (look under the **^** arrow at first). It shows a red **!** because LookFocus hasn't
met your screens yet. Right-click it and choose **Calibrate...**

**One monitor (split into two halves):**
```powershell
.venv\Scripts\lookfocus.exe calibrate --zones 2
.venv\Scripts\pythonw.exe -m lookfocus
```

During calibration, look at each dot **the way you normally would** when working there: turn your head
naturally, don't just move your eyes. Then right-click the icon and tick **Start with Windows** if you want it
around every day.

Prefer a folder you can just run, without Python? See [Standalone build](docs/GUIDE.md#standalone-build).

## The tray icon

| | means |
|---|---|
| <img src="docs/media/icon-active.png" width="24" alt="green eye"> | Watching. Switches when you face another screen. |
| <img src="docs/media/icon-no_face.png" width="24" alt="amber ring"> | Can't see your face right now, so nothing switches. |
| <img src="docs/media/icon-paused.png" width="24" alt="grey pause"> | Paused (by you, or because the PC is locked or asleep). Camera off. |
| <img src="docs/media/icon-calibrating.png" width="24" alt="blue crosshair"> | Calibrating. |
| <img src="docs/media/icon-error.png" width="24" alt="red exclamation mark"> | Needs you. Hover over it to see why (usually: calibrate). |

Right-click for **Pause**, **Recalibrate**, **Choose camera**, **Debug view** (see what LookFocus sees),
**Open config**, **Start with Windows** and **Quit**.

## Getting the best results

- **Keep the camera still.** If you move the laptop, the camera or a monitor, recalibrate. It takes a minute.
- **Put the camera in the middle**, above the screens, where it can see your face whichever screen you look
  at.
- **Give your screens some angle.** Two monitors side by side are easy. Two halves of a small laptop screen
  are hard, because you barely turn your head between them.
- **Light your face from the front.** A bright window behind you turns you into a silhouette.
- **It follows one face.** If someone leans in closer to the camera than you, it may follow them.

Something feels off? Right-click the icon, open **Debug view**, and look at the screens one by one: you'll see
exactly what LookFocus sees.

## Make it yours

Right-click → **Open config**, change a number, save. It takes effect within a few seconds.

| If... | change |
|---|---|
| it switches on quick glances | `dwell_ms` from 300 to 500 |
| it feels slow | `dwell_ms` to 200 |
| it flips back and forth near the edge between screens | `hysteresis` from 2 to 3 or 4 |
| it switches when you look at your phone | `none_margin` from 6 to 4 |
| you want the mouse pointer to follow too | `move_cursor_on_switch` to `true` |
| you want a different pause shortcut | `hotkey`, e.g. `"ctrl+alt+p"` |

Every setting is explained in the [guide](docs/GUIDE.md#settings).

## FAQ

**Does it track my eyes?**
No, it follows where your head points. That works with an ordinary webcam and means a glance from the corner
of your eye doesn't move anything.

**Does it record or upload video?**
No. Images are processed in memory and thrown away immediately. LookFocus never connects to the internet. At
startup it checks its face-tracking library and model, and refuses to run a version that could phone home.

**Is it a keylogger?**
No. It uses the same "was there any input recently" signal Windows uses for idle timers. It never sees which
keys you press.

**What happens on a video call?**
Normally only one app can use the camera, so LookFocus steps aside when a call starts and comes back when it
ends. It tells you when it does. On recent Windows 11 versions you can turn on *"Allow multiple apps to use
camera at the same time"*, and then both work together. Tray menu → **Camera sharing...** checks it for you.

**Will it steal focus while I'm typing?**
It waits longer while you type and never switches while you use the mouse. It only acts when you clearly look
at another screen.

**Some windows won't let it switch away. Why?**
Windows protects programs running as administrator. LookFocus can switch *to* them but not *away* from them,
and tells you once. If you live in admin windows, start it with
`.venv\Scripts\pythonw.exe -m lookfocus tray --elevated`.

**Does it slow my PC down?**
On the test laptop it uses about 6-8% of the processor while it sees you, about 1% when you're away, and
nothing while paused.

**Mac or Linux? Windows 10?**
Windows 11 only. Windows 10 might work but hasn't been tested.

**How do I get rid of it?**
Untick **Start with Windows**, choose **Quit**, delete this folder and `%APPDATA%\LookFocus`.

## Good to know

- It picks the **screen**, not a window within a screen. Split panes on one screen are up to you.
- If the camera or a monitor moves, it needs a fresh calibration.
- It has been tested on one laptop with one monitor split into zones. **Setups with several real monitors
  haven't been tested yet**, especially monitors with different scaling or resolutions, stacked or
  portrait monitors, docking and undocking, external USB cameras, and Windows 10. Feedback welcome.

The full list, with details, is in the [guide](docs/GUIDE.md#known-limitations).

## More

- **[User guide](docs/GUIDE.md)**: every setting, troubleshooting, the standalone build, uninstalling,
  developer notes.
- **[DECISIONS.md](DECISIONS.md)**: why it works the way it does, with the measurements behind each choice.
