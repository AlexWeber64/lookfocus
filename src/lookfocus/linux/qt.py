"""Qt plumbing for the Linux UI (tray icon, calibration overlay, debug view, message boxes).

Qt widgets live on the main thread only, but the engine, the calibration and the debug view run on worker
threads (as on Windows, where each owns its own Win32 window). `call_in_main` runs a function on the Qt
main thread and, by default, waits for its result; called on the main thread it just runs it.

Only QtCore / QtGui / QtWidgets are imported: never QtNetwork (privacy invariant, checked by a test).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox

APP_ID = "lookfocus"

MB_OK, MB_YESNO, MB_ICONWARNING = 0x0, 0x4, 0x30
IDOK, IDYES, IDNO = 1, 6, 7


class _Invoker(QObject):
    run = Signal(object)

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        fn()


_app: QApplication | None = None
_invoker: _Invoker | None = None


def app() -> QApplication:
    """The process's QApplication; created on first use, which must be on the main thread."""
    global _app, _invoker
    if _app is None:
        existing = QApplication.instance()
        if existing is None:
            if threading.current_thread() is not threading.main_thread():
                raise RuntimeError("the Qt UI must be started from the main thread")
            os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")
            QApplication.setDesktopFileName(APP_ID)
            existing = QApplication([APP_ID])
            existing.setApplicationName("LookFocus")
            existing.setQuitOnLastWindowClosed(False)  # closing the overlay or debug view never quits
        _app = existing  # type: ignore[assignment]
        _invoker = _Invoker()
        _invoker.run.connect(_invoker._run, Qt.ConnectionType.QueuedConnection)
    return _app


def on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def call_in_main(fn: Callable, *, wait: bool = True, timeout: float | None = None):
    if on_main_thread():
        app()
        return fn()
    if _invoker is None:
        raise RuntimeError("Qt isn't initialised (qt.app() must run on the main thread first)")
    done = threading.Event()
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # re-raised in the calling thread
            box["error"] = e
        finally:
            done.set()

    _invoker.run.emit(run)
    if not wait:
        return None
    if not done.wait(timeout):
        raise TimeoutError("the Qt main thread didn't respond")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def message_box(text: str, flags: int = MB_OK) -> int:
    """Win32 MessageBox-style dialog (same flags and return codes, so tray.py code stays shared)."""

    def show() -> int:
        box = QMessageBox()
        box.setWindowTitle("LookFocus")
        box.setText(text)
        box.setIcon(QMessageBox.Icon.Warning if flags & MB_ICONWARNING else QMessageBox.Icon.Information)
        if flags & MB_YESNO:
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            return IDYES if box.exec() == QMessageBox.StandardButton.Yes else IDNO
        box.exec()
        return IDOK

    return call_in_main(show)


def pil_to_pixmap(img) -> QPixmap:
    rgba = img.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    q = QImage(data, rgba.width, rgba.height, 4 * rgba.width, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(q.copy())


def bgr_to_image(frame) -> QImage:
    h, w = frame.shape[:2]
    return QImage(frame.data, w, h, int(frame.strides[0]), QImage.Format.Format_BGR888).copy()
