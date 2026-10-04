LookFocus {version}
==================

Look at a monitor and just type. LookFocus notices which screen you're facing (through your webcam) and
puts your keyboard there. It runs in the Windows tray and never goes online.


START
-----
1. Double-click LookFocus.exe.
   The first time, Windows may say "Windows protected your PC", because the app isn't signed by a company.
   Choose "More info", then "Run anyway".
2. A welcome message offers to set things up. Say Yes and look at each dot as it appears, the way you
   normally would. It takes about a minute.
3. That's it. Look at a screen and type.

The LookFocus icon sits in the tray at the bottom right (it may hide under the ^ arrow at first; drag it
onto the taskbar to keep it visible). Right-click it for everything else, including "Start with Windows".

Pause or resume any time: Ctrl+Alt+Shift+G, or click the tray icon.


ONE MONITOR?
------------
LookFocus can split it into a left and a right half that work like two screens. The welcome message
offers this; later you can change it under the tray menu > Screen zones.


PRIVATE BY DESIGN
-----------------
- Camera images stay in memory: never saved, never sent anywhere.
- No internet connection, no account, no telemetry.
- No keylogging: it notices that you typed, never what.


GOOD TO KNOW
------------
- Keep this whole folder together: LookFocus.exe needs the _internal folder next to it.
- You can move the folder anywhere. If you use Start with Windows, start LookFocus once from the new place.
- lookfocus-cli.exe is for the command line (for example "lookfocus-cli run --dry-run --stats").
- Your settings, calibration and logs live in %APPDATA%\LookFocus.
- Some antivirus programs distrust apps made this way (Python apps packed with PyInstaller). It's a known
  false positive. You can also run LookFocus from its source code instead.


UNINSTALL
---------
1. Right-click the tray icon: untick "Start with Windows", then "Quit".
2. Delete this folder and %APPDATA%\LookFocus.


Third-party software notices are in the third-party-licenses folder.
