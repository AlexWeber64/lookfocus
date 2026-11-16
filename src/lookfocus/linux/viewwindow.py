"""The debug view's window on Linux (Qt), standing in for OpenCV's HighGUI window on Windows.

The pip OpenCV wheels draw HighGUI windows with their own bundled Qt 5 over X11, which would put a second
Qt in the process next to PySide6 and need XWayland; a QLabel showing the same canvas avoids both. Keys
come from key events to this window only (as with cv2.waitKey), never from anywhere else.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QLabel

from lookfocus.linux import qt


class _Label(QLabel):
    def __init__(self, owner: ImageWindow, title: str) -> None:
        super().__init__()
        self.owner = owner
        self.setWindowTitle(title)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

    def keyPressEvent(self, e) -> None:
        text = e.text()
        if e.key() == Qt.Key.Key_Escape:
            self.owner.push_key(27)
        elif len(text) == 1 and text.isprintable():
            self.owner.push_key(ord(text.lower()))

    def closeEvent(self, e) -> None:
        self.owner.closed_flag = True
        super().closeEvent(e)


class ImageWindow:
    def __init__(self, title: str) -> None:
        self.closed_flag = False
        self._keys: list[int] = []
        self._lock = threading.Lock()
        self._w: _Label = qt.call_in_main(lambda: _Label(self, title))
        self._shown = False

    def push_key(self, key: int) -> None:
        with self._lock:
            self._keys.append(key)

    def show(self, canvas) -> None:
        image = qt.bgr_to_image(canvas)  # copied here: the caller redraws the canvas right away

        def put() -> None:
            self._w.setPixmap(QPixmap.fromImage(image))
            if not self._shown:
                self._shown = True
                self._w.adjustSize()
                self._w.show()

        # Not waited for: the tray's main thread may be joining this viewer thread (Quit, Close debug view),
        # and the image is already a copy.
        qt.call_in_main(put, wait=False)

    def poll(self) -> tuple[int, bool]:
        """(key or 255, window closed) like cv2.waitKey(1) and WND_PROP_VISIBLE."""
        if qt.on_main_thread():
            QApplication.processEvents()
        with self._lock:
            key = self._keys.pop(0) if self._keys else 255
        return key, self.closed_flag

    def close(self) -> None:
        w = self._w
        qt.call_in_main(lambda: (w.close(), w.deleteLater()), wait=False)
