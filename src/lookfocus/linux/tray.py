"""The tray app on KDE Plasma: tray.TrayApp's logic, shown through Qt's QSystemTrayIcon.

tray.py builds its menu with pystray's Menu / MenuItem, which are plain Python objects (text, checked and
visibility computed on demand). QtTrayIcon implements the few pystray.Icon members TrayApp uses (icon,
title, menu, update_menu, notify, run, stop, visible) and renders that menu into a QMenu, rebuilt each time
it opens, so the status line and the camera list are always current, like the Windows tray's menu.
On Plasma, Qt publishes the icon as a StatusNotifierItem and notifications go to Plasma's notification
centre. pystray's own Linux backends need GTK / AppIndicator or an X11 tray, so they aren't used.

Threads: Qt's event loop runs on the main thread, and menu actions run there (as pystray's do on Windows);
the engine and other threads change the icon, title and notifications through signals.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess

import pystray
from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from lookfocus import tray as T
from lookfocus.linux import qt

log = logging.getLogger("lookfocus.tray")

ICON_SIZES = (16, 22, 24, 32, 48, 64)


def qicon_for(state: str) -> QIcon:
    icon = QIcon()
    for s in ICON_SIZES:
        icon.addPixmap(qt.pil_to_pixmap(T.make_icon(state, s)))
    return icon


class QtTrayIcon(QObject):
    _set_icon = Signal(object)
    _set_title = Signal(str)
    _rebuild = Signal()
    _notify = Signal(str, str)
    _quit = Signal()

    def __init__(self, name: str, icon, title: str, menu: pystray.Menu, states: dict | None = None) -> None:
        super().__init__()
        qt.app()
        self.name = name
        self._states = states or {}  # state -> the PIL image TrayApp uses for it (drawn again per size)
        self.menu = menu
        self._title = title
        self._icon_img = icon
        self._state_icons: dict[int, QIcon] = {}
        self.tray = QSystemTrayIcon()
        self.tray.setToolTip(title)
        self.tray.setIcon(self._qicon(icon))
        self.qmenu = QMenu()
        self.qmenu.aboutToShow.connect(self._build_menu)
        self.tray.setContextMenu(self.qmenu)
        self.tray.activated.connect(self._activated)
        self._set_icon.connect(self._apply_icon)
        self._set_title.connect(self.tray.setToolTip)
        self._rebuild.connect(self._build_menu)
        self._notify.connect(self._show_message)
        self._quit.connect(lambda: qt.app().quit())
        self._build_menu()

    def _qicon(self, img) -> QIcon:
        key = id(img)
        if key not in self._state_icons:
            state = next((s for s, i in self._states.items() if i is img), None)
            self._state_icons[key] = qicon_for(state) if state else QIcon(qt.pil_to_pixmap(img))
        return self._state_icons[key]

    # --- pystray.Icon API used by TrayApp ---
    @property
    def icon(self):
        return self._icon_img

    @icon.setter
    def icon(self, img) -> None:
        self._icon_img = img
        self._set_icon.emit(img)

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, text: str) -> None:
        self._title = text
        self._set_title.emit(text)

    @property
    def visible(self) -> bool:
        return self.tray.isVisible()

    @visible.setter
    def visible(self, value: bool) -> None:
        qt.call_in_main(lambda: self.tray.setVisible(value))

    def update_menu(self) -> None:
        self._rebuild.emit()

    def notify(self, message: str, title: str = "") -> None:
        self._notify.emit(title, message)

    def run(self, setup=None) -> None:
        app = qt.app()
        self.tray.show()
        if setup is not None:
            QTimer.singleShot(0, lambda: setup(self))
        app.exec()

    def stop(self) -> None:
        self._quit.emit()

    # --- Qt side (main thread) ---
    @Slot(object)
    def _apply_icon(self, img) -> None:
        self.tray.setIcon(self._qicon(img))

    @Slot(str, str)
    def _show_message(self, title: str, message: str) -> None:
        self.tray.showMessage(title or "LookFocus", message, self._qicon(self._icon_img), 8000)

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:  # left click: the default item, as on Windows
            for item in self.menu.items:
                if item.default and item.enabled:
                    self._run(item)
                    return

    def _run(self, item) -> None:
        try:
            item(self)
        except Exception:
            log.exception("menu action failed")

    @Slot()
    def _build_menu(self) -> None:
        self.qmenu.clear()
        self._fill(self.qmenu, self.menu)

    def _fill(self, qmenu: QMenu, menu: pystray.Menu) -> None:
        for item in menu.items:
            if item is pystray.Menu.SEPARATOR:
                qmenu.addSeparator()
                continue
            if not item.visible:
                continue
            if item.submenu is not None:
                sub = qmenu.addMenu(item.text)
                sub.setEnabled(item.enabled)
                self._fill(sub, item.submenu)
                continue
            action = QAction(item.text, qmenu)
            action.setEnabled(item.enabled)
            checked = item.checked
            if checked is not None:
                action.setCheckable(True)
                action.setChecked(bool(checked))
            if item.default:
                f = action.font()
                f.setBold(True)
                action.setFont(f)
            action.triggered.connect(lambda _=False, it=item: self._run(it))
            qmenu.addAction(action)


class LinuxTrayApp(T.TrayApp):
    def _create_icon(self, name, image, title, menu):
        return QtTrayIcon(name, image, title, menu, states=self._icons)

    def open_config(self) -> None:
        from lookfocus import config as K

        if not self.cfg_path.exists():
            K.save(self.cfg, self.cfg_path)
        try:
            subprocess.Popen(["xdg-open", str(self.cfg_path)])
        except OSError as e:
            self.notify("Open config", f"Couldn't open {self.cfg_path}: {e}")

    def _on_system(self, event: str) -> None:
        if event == "hotkey_kde":
            self.notify(
                "Change the shortcut in System Settings",
                "On KDE Plasma the pause shortcut is a global shortcut: System Settings > Keyboard > "
                "Shortcuts > KWin > LookFocus: pause or resume.",
            )
            return
        super()._on_system(event)

    def camera_sharing_dialog(self) -> None:
        T.message_box(
            "On Linux, LookFocus gets the camera through PipeWire, which shares it: video calls and "
            "LookFocus can use the camera at the same time, so there is nothing to set up.\n\n"
            "(With the V4L2 camera backend, another app may not get the camera while LookFocus uses it.)"
        )


def main() -> int:
    from lookfocus.linux import kwin
    from lookfocus.linux.sysevents import claim_single_instance

    qt.app()
    if not claim_single_instance():
        T.message_box("LookFocus is already running (see the tray icon).")
        return 0
    from lookfocus import config as K
    from lookfocus import paths

    cfg, _ = K.load(paths.data_dir() / "config.json")
    with contextlib.suppress(ValueError):
        kwin.configure(K.kde_key_sequence(cfg.hotkey))
    try:
        kwin.get_bridge()
    except kwin.BridgeError as e:
        T.message_box(str(e), qt.MB_ICONWARNING)
        return 1
    try:
        LinuxTrayApp().run()
    finally:
        kwin.shutdown()
    return 0
