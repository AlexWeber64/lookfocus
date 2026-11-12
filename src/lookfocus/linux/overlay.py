"""Calibration overlay on Wayland (Qt): the counterpart of overlay.py, with the same API.

Wayland clients can't place a window at a position, but they can make one full-screen on a chosen screen.
So the overlay covers the whole monitor holding the logical display: the display's own area is drawn like
the Windows overlay (dark, title, the dot), and with zones the rest of the monitor is dimmed, not hidden.

Thread-agnostic: every Qt call goes through qt.call_in_main, so the CLI (calibration on the main thread,
which `pump` keeps responsive) and the tray (calibration on a worker thread while Qt's loop runs on the
main thread) share this class. Esc in the overlay (a normal key event to our own window, not a hook) or
closing it sets `cancelled`. The window activates itself through the KWin bridge, because KWin's focus
stealing prevention may keep a new window from a background app unfocused, and Esc needs the focus.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time

from PySide6.QtCore import QEventLoop, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from lookfocus.displays import Rect
from lookfocus.linux import qt

log = logging.getLogger(__name__)

BG = (18, 18, 22)
TEXT = (225, 225, 230)
DIM = (150, 150, 160)
WAITING = (255, 196, 0)
MEASURING = (60, 220, 110)
TITLE = "LookFocus calibration"


def _color(c, alpha: int = 255) -> QColor:
    return QColor(c[0], c[1], c[2], alpha)


class _Widget(QWidget):
    def __init__(self, owner: Overlay) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.owner = owner
        self.zone = QRect()
        self._closing = False
        self.setWindowTitle(TITLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.BlankCursor)  # the pointer would pull the eye away from the dot
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.update)

    def show_on(self, rect: Rect) -> None:
        cx, cy = rect.center
        screen = next(
            (s for s in QGuiApplication.screens() if s.geometry().contains(int(cx), int(cy))),
            QGuiApplication.primaryScreen(),
        )
        g = screen.geometry()
        self.zone = QRect(rect.left - g.x(), rect.top - g.y(), rect.width, rect.height)
        if self.isVisible() and self.screen() is not screen:
            self.hide()  # a full-screen window only moves to another output when shown again
        self.setScreen(screen)
        self.setGeometry(g)
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.timer.start()
        self.update()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.owner.cancelled = True

    def closeEvent(self, e) -> None:
        if not self._closing:
            self.owner.cancelled = True
        super().closeEvent(e)

    def paintEvent(self, _e) -> None:
        o = self.owner
        with o.lock:
            dot, color, title, lines, progress = o.dot, o.color, o.title, list(o.lines), o.progress
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        full, z = self.rect(), self.zone if not self.zone.isEmpty() else self.rect()
        if z != full:
            p.fillRect(full, QColor(0, 0, 0, 150))
        p.fillRect(z, _color(BG))
        tf = QFont()
        tf.setPixelSize(26)
        tf.setWeight(QFont.Weight.DemiBold)
        p.setFont(tf)
        p.setPen(_color(TEXT))
        p.drawText(QRectF(z.x() + 24, z.y() + 28, z.width() - 48, 40), Qt.AlignmentFlag.AlignHCenter, title)
        bf = QFont()
        bf.setPixelSize(17)
        p.setFont(bf)
        p.setPen(_color(DIM))
        y = z.y() + 28 + 46
        for line in lines:
            p.drawText(QRectF(z.x() + 24, y, z.width() - 48, 26), Qt.AlignmentFlag.AlignHCenter, line)
            y += 26
        if dot is not None:
            cx, cy = z.x() + dot[0] * (z.width() - 1), z.y() + dot[1] * (z.height() - 1)
            pulse = 0.5 + 0.5 * math.sin((time.monotonic() - o.t0) * 5.0)
            outer, inner = 16 + 10 * pulse, 5
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_color(color))
            p.drawEllipse(QPointF(cx, cy), outer, outer)
            p.setBrush(QColor(255, 255, 255))
            p.drawEllipse(QPointF(cx, cy), inner, inner)
            if progress is not None:
                bar_w = 120
                top = cy + outer + 12
                if top + 8 > z.bottom():
                    top = cy - outer - 20
                left = max(z.x(), min(z.x() + z.width() - bar_w, cx - bar_w / 2))
                p.fillRect(QRectF(left, top, bar_w, 6), QColor(60, 60, 70))
                done = bar_w * max(0.0, min(1.0, progress))
                p.fillRect(QRectF(left, top, done, 6), _color(color))
        p.end()

    def close_now(self) -> None:
        self._closing = True
        self.timer.stop()
        self.close()
        self.deleteLater()


class Overlay:
    """One overlay window, reused for each display in turn."""

    def __init__(self) -> None:
        self.cancelled = False
        self.hwnd = None  # no Win32 handle; the overlay focuses itself (see module docstring)
        self.lock = threading.Lock()
        self.dot: tuple[float, float] | None = None
        self.color = WAITING
        self.title = ""
        self.lines: list[str] = []
        self.progress: float | None = None
        self.t0 = time.monotonic()
        self._w: _Widget | None = qt.call_in_main(lambda: _Widget(self))

    def show(self, rect: Rect) -> None:
        w = self._w
        if w is not None:
            qt.call_in_main(lambda: w.show_on(rect))
            threading.Thread(target=self._activate, name="overlay-activate", daemon=True).start()

    def _activate(self) -> None:
        """Ask KWin to give the overlay the keyboard focus (so Esc works)."""
        from lookfocus.linux.kwin import get_bridge

        try:
            bridge = get_bridge()
            for _ in range(10):
                time.sleep(0.05)
                snap = bridge.request("windows")
                mine = [
                    d for d in snap["windows"] if d.get("pid") == os.getpid() and d.get("caption") == TITLE
                ]
                if mine:
                    if snap.get("active") != mine[-1]["id"]:
                        bridge.request("activate", id=mine[-1]["id"])
                    return
        except Exception as e:
            log.debug("couldn't activate the overlay: %s", e)

    def set_scene(self, *, dot=None, color=WAITING, title="", lines=(), progress=None) -> None:
        with self.lock:
            self.dot, self.color, self.title, self.lines, self.progress = (
                dot,
                color,
                title,
                list(lines),
                progress,
            )
        self.redraw()

    def redraw(self) -> None:
        w = self._w
        if w is not None:
            qt.call_in_main(w.update, wait=False) if not qt.on_main_thread() else w.update()

    def pump(self, seconds: float) -> bool:
        """Keep the overlay responsive for `seconds`. Returns False once the user has cancelled."""
        end = time.monotonic() + seconds
        while not self.cancelled:
            left = end - time.monotonic()
            if left <= 0:
                break
            if qt.on_main_thread():
                QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 30)
                time.sleep(min(left, 0.01))
            else:
                time.sleep(min(left, 0.03))  # the Qt loop on the main thread animates the dot
        return not self.cancelled

    def hide(self) -> None:
        w = self._w
        if w is not None:
            qt.call_in_main(w.hide)
            self.pump(0.05)

    def close(self) -> None:
        w, self._w = self._w, None
        if w is not None:
            qt.call_in_main(w.close_now)
