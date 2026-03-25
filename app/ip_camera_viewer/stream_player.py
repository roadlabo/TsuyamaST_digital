from __future__ import annotations

import time
from typing import Optional

import cv2
from PyQt6.QtCore import QMutex, QObject, QThread, pyqtSignal
from PyQt6.QtGui import QImage

from logger import setup_logger

logger = setup_logger(__name__)


class StreamWorker(QThread):
    frame_ready = pyqtSignal(QImage)
    status_changed = pyqtSignal(str)

    def __init__(self, rtsp_url: str, target_fps: float = 5.0, reconnect_seconds: float = 4.0, parent: QObject | None = None):
        super().__init__(parent)
        self.rtsp_url = rtsp_url
        self.target_fps = target_fps
        self.reconnect_seconds = reconnect_seconds
        self._running = False
        self._mutex = QMutex()

    def stop(self) -> None:
        self._mutex.lock()
        self._running = False
        self._mutex.unlock()

    def _is_running(self) -> bool:
        self._mutex.lock()
        running = self._running
        self._mutex.unlock()
        return running

    def run(self) -> None:
        self._mutex.lock()
        self._running = True
        self._mutex.unlock()

        frame_interval = 1.0 / self.target_fps if self.target_fps > 0 else 0
        while self._is_running():
            if not self.rtsp_url:
                self.status_changed.emit("ERROR")
                logger.warning("RTSP URL is empty. skip stream start.")
                return

            cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                self.status_changed.emit("ERROR")
                logger.warning("Failed to connect stream: %s", self.rtsp_url)
                cap.release()
                self._sleep_with_interrupt(self.reconnect_seconds)
                continue

            self.status_changed.emit("OK")
            last_emit = 0.0

            while self._is_running():
                ok, frame = cap.read()
                if not ok or frame is None:
                    self.status_changed.emit("ERROR")
                    logger.warning("Stream dropped. reconnecting: %s", self.rtsp_url)
                    break

                now = time.monotonic()
                if frame_interval > 0 and (now - last_emit) < frame_interval:
                    continue
                last_emit = now

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb.shape
                bytes_per_line = ch * w
                image = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()
                self.frame_ready.emit(image)

            cap.release()
            self._sleep_with_interrupt(self.reconnect_seconds)

    def _sleep_with_interrupt(self, seconds: float) -> None:
        until = time.monotonic() + max(0.0, seconds)
        while self._is_running() and time.monotonic() < until:
            self.msleep(100)


class StreamPlayer(QObject):
    frame_ready = pyqtSignal(QImage)
    status_changed = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._worker: Optional[StreamWorker] = None

    def start(self, rtsp_url: str, target_fps: float = 5.0) -> None:
        self.stop()
        if not rtsp_url:
            self.status_changed.emit("ERROR")
            return
        self._worker = StreamWorker(rtsp_url=rtsp_url, target_fps=target_fps, parent=self)
        self._worker.frame_ready.connect(self.frame_ready.emit)
        self._worker.status_changed.connect(self.status_changed.emit)
        self._worker.start()

    def stop(self) -> None:
        if not self._worker:
            return
        self._worker.stop()
        self._worker.wait(3000)
        self._worker.deleteLater()
        self._worker = None
