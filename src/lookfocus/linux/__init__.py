"""Linux (KDE Plasma 6, Wayland) backends: the counterparts of the Win32 modules one level up.

KWin is the only party on Wayland that knows which window is active, where windows and the pointer are, and
that may activate a window, so window management goes through a KWin script (kwin.py). Input *activity*
(never content) comes from the Wayland idle-notify protocol (wayland.py), cameras from PipeWire
(camera.py), and the tray, overlay and dialogs use Qt (Plasma's own toolkit). See DECISIONS.md D-050.
"""
