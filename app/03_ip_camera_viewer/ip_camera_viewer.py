from __future__ import annotations

import json
import logging
import os
import sys
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import cv2
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from PyQt6.QtCore import QMutex, QObject, QPoint, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import (
    QCloseEvent,
    QGuiApplication,
    QImage,
    QMouseEvent,
    QPainter,
    QPixmap,
    QResizeEvent,
    QScreen,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


def setup_logger(name: str = "ip_camera_viewer") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    log_dir = Path(__file__).resolve().parents[2] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ip_camera_viewer.log"

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger


logger = setup_logger(__name__)
GROUP_TILE_WIDTH = 900


def open_camera_settings_with_login(camera_config: dict, common_auth: dict, parent: QWidget | None = None) -> bool:
    url = (camera_config.get("settings_url") or camera_config.get("web_url") or "").strip()
    username = common_auth.get("username", "").strip()
    password = common_auth.get("password", "").strip()

    if not url:
        QMessageBox.warning(parent, "URLエラー", "設定画面URLが未設定です。")
        logger.warning("Settings URL is not set: camera_id=%s", camera_config.get("id", "unknown"))
        return False

    if not username or not password:
        logger.info(
            "Common auth is not configured. Fallback to default browser: camera_id=%s url=%s",
            camera_config.get("id", "unknown"),
            url,
        )
        return open_camera_settings(url, parent=parent)

    try:
        options = Options()
        options.add_argument("--start-maximized")

        driver = webdriver.Edge(options=options)
        driver.get(url)

        wait = WebDriverWait(driver, 10)

        user_input = wait.until(EC.presence_of_element_located((By.ID, "UserName")))
        user_input.clear()
        user_input.send_keys(username)

        pass_input = wait.until(EC.presence_of_element_located((By.ID, "Password")))
        pass_input.clear()
        pass_input.send_keys(password)

        login_button = wait.until(EC.element_to_be_clickable((By.NAME, "B1")))
        login_button.click()

        logger.info("Camera settings auto-login started: camera_id=%s url=%s", camera_config.get("id", "unknown"), url)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Auto-login failed. Fallback to default browser: camera_id=%s url=%s error=%s",
            camera_config.get("id", "unknown"),
            url,
            exc,
        )
        return open_camera_settings(url, parent=parent)


def open_camera_settings(url: str, parent: QWidget | None = None) -> bool:
    if not url:
        QMessageBox.warning(parent, "URLエラー", "設定画面URLが未設定です。")
        return False

    try:
        opened = webbrowser.open(url)
        if not opened:
            raise RuntimeError("既定ブラウザでURLを開けませんでした")
        logger.info("Opened camera settings URL: %s", url)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to open URL %s: %s", url, exc)
        QMessageBox.critical(parent, "URL起動失敗", f"設定画面を開けませんでした。\nURL: {url}\n{exc}")
        return False


class StreamWorker(QThread):
    frame_ready = pyqtSignal(QImage)
    status_changed = pyqtSignal(str)

    def __init__(self, rtsp_url: str, target_fps: float = 5.0, reconnect_seconds: float = 4.0, camera_label: str = "", parent: QObject | None = None):
        super().__init__(parent)
        self.rtsp_url = rtsp_url
        self.target_fps = target_fps
        self.reconnect_seconds = reconnect_seconds
        self.camera_label = camera_label or "cam?"
        self._running = False
        self._mutex = QMutex()
        self._cap: cv2.VideoCapture | None = None

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

        try:
            periodic_reopen_sec = 1800.0
            reconnect_count = 0
            while self._is_running():
                if not self.rtsp_url:
                    self.status_changed.emit("ERROR")
                    logger.warning("RTSP URL is empty. skip stream start.")
                    return

                cap: cv2.VideoCapture | None = None
                try:
                    cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
                    self._mutex.lock()
                    self._cap = cap
                    self._mutex.unlock()

                    if not cap.isOpened():
                        self.status_changed.emit("ERROR")
                        logger.warning("Failed to connect stream: %s", self.rtsp_url)
                        continue

                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    self.status_changed.emit("OK")
                    opened_at = time.monotonic()
                    read_fail_count = 0
                    fps_frames = 0
                    fps_last_ts = time.monotonic()
                    current_fps = 0.0
                    last_success_read_at = 0.0
                    last_view_log_at = 0.0

                    while self._is_running():
                        if (time.monotonic() - opened_at) >= periodic_reopen_sec:
                            logger.info("[VIEW] %s periodic reopen", self.camera_label)
                            break
                        try:
                            ok, frame = cap.read()
                        except cv2.error as exc:
                            self.status_changed.emit("ERROR")
                            logger.warning("OpenCV read failed. reconnecting: %s error=%s", self.rtsp_url, exc)
                            break
                        except Exception as exc:  # noqa: BLE001
                            self.status_changed.emit("ERROR")
                            logger.warning("Unexpected read error. reconnecting: %s error=%s", self.rtsp_url, exc)
                            break

                        if not ok or frame is None:
                            read_fail_count += 1
                            if read_fail_count >= 3:
                                self.status_changed.emit("ERROR")
                                logger.warning("Stream dropped repeatedly. reconnecting: %s", self.rtsp_url)
                                break
                            continue
                        read_fail_count = 0
                        last_success_read_at = time.monotonic()

                        now = time.monotonic()
                        fps_frames += 1
                        if (now - fps_last_ts) >= 1.0:
                            current_fps = fps_frames / max(1e-6, (now - fps_last_ts))
                            fps_frames = 0
                            fps_last_ts = now
                        if (now - last_view_log_at) >= 60.0:
                            age_ms = 0.0 if last_success_read_at <= 0.0 else (now - last_success_read_at) * 1000.0
                            logger.info("[VIEW] %s fps=%.2f age=%.0fms reconnect=%d", self.camera_label, current_fps, age_ms, reconnect_count)
                            last_view_log_at = now

                        try:
                            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        except cv2.error as exc:
                            logger.warning("cvtColor failed. skip frame: %s error=%s", self.rtsp_url, exc)
                            continue
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Unexpected frame conversion error. skip frame: %s error=%s", self.rtsp_url, exc)
                            continue

                        h, w, ch = rgb.shape
                        bytes_per_line = ch * w
                        image = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()
                        self.frame_ready.emit(image)
                finally:
                    if cap is not None:
                        cap.release()
                    self._mutex.lock()
                    self._cap = None
                    self._mutex.unlock()

                reconnect_count += 1
                self._sleep_with_interrupt(self.reconnect_seconds)
        except Exception as exc:  # noqa: BLE001
            self.status_changed.emit("ERROR")
            logger.exception("StreamWorker crashed: %s", exc)

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
        self._stopping = False

    def start(self, rtsp_url: str, target_fps: float = 5.0, camera_label: str = "") -> None:
        self.stop()
        if not rtsp_url:
            self.status_changed.emit("ERROR")
            return
        self._worker = StreamWorker(rtsp_url=rtsp_url, target_fps=target_fps, camera_label=camera_label, parent=self)
        self._worker.frame_ready.connect(self.frame_ready.emit)
        self._worker.status_changed.connect(self.status_changed.emit)
        self._worker.start()

    def stop(self) -> None:
        if self._stopping:
            return
        worker = self._worker
        if worker is None:
            return

        self._stopping = True
        try:
            worker.stop()
            worker.quit()
            finished = worker.wait(800)
            if not finished:
                logger.warning("Stream worker did not finish in timeout. force terminate.")
                worker.terminate()
                logger.warning("force terminate executed for stream worker")
                worker.wait(500)
            worker.deleteLater()
            self._worker = None
        finally:
            self._stopping = False


class ZoomPanVideoWidget(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._zoom = 1.0
        self._min_zoom = 1.0
        self._max_zoom = 4.0
        self._offset = QPoint(0, 0)
        self._dragging = False
        self._drag_start = QPoint()

    def set_image(self, image: QImage) -> None:
        self._pixmap = QPixmap.fromImage(image)
        self.update()

    def reset_view(self) -> None:
        self._zoom = self._min_zoom
        self._offset = QPoint(0, 0)
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_view()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta == 0:
            return
        step = 1.15 if delta > 0 else 1 / 1.15
        self._zoom = max(self._min_zoom, min(self._max_zoom, self._zoom * step))
        if self._zoom <= self._min_zoom:
            self._offset = QPoint(0, 0)
        else:
            self._offset = self._clamp_offset(self._offset)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._zoom > self._min_zoom:
            self._dragging = True
            self._drag_start = event.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._dragging:
            delta = event.pos() - self._drag_start
            self._drag_start = event.pos()
            self._offset = self._clamp_offset(self._offset + delta)
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._pixmap is None or self._pixmap.isNull():
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "カメラを選択してください")
            return

        pix_w = max(1, self._pixmap.width())
        pix_h = max(1, self._pixmap.height())
        view_w = max(1, self.width())
        view_h = max(1, self.height())

        fit_scale = min(view_w / pix_w, view_h / pix_h)
        display_scale = fit_scale * self._zoom
        scaled_w = max(1, int(pix_w * display_scale))
        scaled_h = max(1, int(pix_h * display_scale))

        if self._zoom <= self._min_zoom:
            self._offset = QPoint(0, 0)
        else:
            self._offset = self._clamp_offset(self._offset, scaled_w=scaled_w, scaled_h=scaled_h)

        target_x = (view_w - scaled_w) // 2 + self._offset.x()
        target_y = (view_h - scaled_h) // 2 + self._offset.y()

        painter.drawPixmap(target_x, target_y, scaled_w, scaled_h, self._pixmap)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        if self._zoom > self._min_zoom:
            self._offset = self._clamp_offset(self._offset)
        super().resizeEvent(event)

    def _clamp_offset(self, offset: QPoint, scaled_w: int | None = None, scaled_h: int | None = None) -> QPoint:
        if self._pixmap is None or self._pixmap.isNull():
            return QPoint(0, 0)

        pix_w = max(1, self._pixmap.width())
        pix_h = max(1, self._pixmap.height())
        view_w = max(1, self.width())
        view_h = max(1, self.height())

        if scaled_w is None or scaled_h is None:
            fit_scale = min(view_w / pix_w, view_h / pix_h)
            display_scale = fit_scale * self._zoom
            scaled_w = max(1, int(pix_w * display_scale))
            scaled_h = max(1, int(pix_h * display_scale))

        max_x = max(0, (scaled_w - view_w) // 2)
        max_y = max(0, (scaled_h - view_h) // 2)

        clamped_x = max(-max_x, min(max_x, offset.x()))
        clamped_y = max(-max_y, min(max_y, offset.y()))
        return QPoint(clamped_x, clamped_y)


class AspectRatioVideoLabel(QLabel):
    def __init__(self, ratio_width: int = 16, ratio_height: int = 9, parent: QWidget | None = None):
        super().__init__(parent)
        self.ratio_width = ratio_width
        self.ratio_height = ratio_height

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        if self.ratio_width <= 0:
            return width
        return int(width * self.ratio_height / self.ratio_width)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        parent = self.parentWidget()
        if parent is None:
            return
        target_width = max(1, parent.width())
        target_height = self.heightForWidth(target_width)
        if self.height() != target_height:
            self.setFixedHeight(target_height)


class CameraTile(QWidget):
    clicked = pyqtSignal(dict)
    first_frame_ready = pyqtSignal(str)

    def __init__(self, camera_config: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.camera_config = camera_config
        self._last_image: QImage | None = None
        self._frame_count = 0
        self._fps = 0.0
        self._last_fps_update = time.monotonic()
        self._reconnect_count = 0
        self._last_status = "INIT"
        self._status_text = "接続待機"
        self._last_frame_monotonic = 0.0
        self._stream_type = "SUB"
        self._has_emitted_first_frame = False
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(1000)
        self._update_timer.timeout.connect(self._update_info_panel)
        self._update_timer.start()

        self.setFixedWidth(352)

        self.setStyleSheet(
            """
            QWidget { background-color: #1f1f22; border: 1px solid #38383c; color: #e4e4e4; }
            QLabel#title { font-size: 12px; font-weight: bold; padding: 2px 4px; color: #f0f0f0; }
            QLabel#info { font-size: 10px; padding: 1px 4px; color: #c7c7c7; }
            """
        )

        self.title_label = QLabel(camera_config.get("name", camera_config.get("id", "Unknown")))
        self.title_label.setObjectName("title")
        self.info_label = QLabel("")
        self.info_label.setObjectName("info")

        self.video_container = QWidget()
        self.video_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.video_label = AspectRatioVideoLabel(16, 9, self.video_container)
        self.video_label.setText("読み込み中...")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.video_label.setScaledContents(False)
        self.video_label.setStyleSheet("background: #000000; border: 1px solid #303236;")
        video_layout = QVBoxLayout(self.video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.addWidget(self.video_label)

        self.status_label = QLabel("接続待機")
        self.status_label.setObjectName("info")
        self.status_label.setStyleSheet("color: #ffb57a;")

        self.layout_main = QVBoxLayout(self)
        self.layout_main.setContentsMargins(4, 4, 4, 4)
        self.layout_main.setSpacing(2)
        self.layout_main.addWidget(self.title_label)
        self.layout_main.addWidget(self.info_label)
        self.layout_main.addWidget(self.video_container, 1, Qt.AlignmentFlag.AlignTop)
        self.layout_main.addWidget(self.status_label)

        self.stream_player = StreamPlayer(self)
        self.stream_player.frame_ready.connect(self._update_frame)
        self.stream_player.status_changed.connect(self._update_status)

    def start_stream(self) -> None:
        self.stream_player.stop()
        self._last_status = "INIT"
        self._status_text = "接続待機"
        self._last_image = None
        self._fps = 0.0
        self._frame_count = 0
        self._last_fps_update = time.monotonic()
        self._last_frame_monotonic = 0.0
        self._has_emitted_first_frame = False

        if not self.camera_config.get("enabled", True):
            self.show_offline()
            self._update_status("OFF")
            return

        rtsp_sub = self.camera_config.get("rtsp_sub")
        if not rtsp_sub:
            self._update_status("ERROR")
            self.video_label.setText("サブストリーム未設定")
            return
        self._stream_type = "SUB"
        self.stream_player.start(rtsp_sub, target_fps=5.0, camera_label=str(self.camera_config.get("id", "cam?")))

    def stop_stream(self) -> None:
        self.stream_player.stop()

    def dispose(self) -> None:
        self.stop_stream()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.stop_stream()
        super().closeEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.camera_config)
        super().mousePressEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)

    def _update_frame(self, image: QImage) -> None:
        self._last_image = image
        if not self._has_emitted_first_frame:
            self._has_emitted_first_frame = True
            self.first_frame_ready.emit(self.camera_config.get("id", ""))
        self._frame_count += 1
        now = time.monotonic()
        self._last_frame_monotonic = now
        elapsed = now - self._last_fps_update
        if elapsed >= 1.0:
            self._fps = self._frame_count / elapsed
            self._frame_count = 0
            self._last_fps_update = now
        self._set_pixmap(image)

    def _set_pixmap(self, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image)
        self.video_label.setPixmap(
            pixmap.scaled(self.video_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        )

    def show_offline(self) -> None:
        self.setStyleSheet(
            """
            QWidget { background-color: #333333; border: 1px solid #2a2a2a; color: #aaaaaa; }
            QLabel#title { font-size: 14px; font-weight: bold; padding: 4px 6px; }
            QLabel#status { font-size: 12px; color: #aaaaaa; padding: 2px 6px; }
            """
        )
        self.video_label.setPixmap(QPixmap())
        self.video_label.setText("未接続\n(準備中)")

    def _update_status(self, status: str) -> None:
        if self._last_status == "OK" and status != "OK":
            self._reconnect_count += 1
        self._last_status = status
        if status == "OK":
            self._status_text = "ONLINE"
            self.status_label.setText(self._status_text)
            self.status_label.setStyleSheet("color: #74ffce;")
            return

        if status == "OFF":
            self._status_text = "OFFLINE / 未接続"
            self.status_label.setText(self._status_text)
            self.status_label.setStyleSheet("color: #94a4ad;")
        else:
            self._status_text = "RECONNECT / 接続再試行"
            self.status_label.setText(self._status_text)
            self.status_label.setStyleSheet("color: #ffb57a;")

    def set_display_mode(self, group_mode: bool) -> None:
        if group_mode:
            self.setFixedWidth(GROUP_TILE_WIDTH)
            self.layout_main.setContentsMargins(2, 2, 2, 2)
            self.layout_main.setSpacing(0)
        else:
            self.setFixedWidth(352)
            self.layout_main.setContentsMargins(4, 4, 4, 4)
            self.layout_main.setSpacing(2)
        self.video_label.setFixedHeight(self.video_label.heightForWidth(max(1, self.video_container.width())))
        self.updateGeometry()

    def _update_info_panel(self) -> None:
        cam_ip = self.camera_config.get("ip", "-")
        resolution = "-"
        if self._last_image is not None:
            resolution = f"{self._last_image.width()}×{self._last_image.height()}"
        age_ms = 0.0
        if self._last_frame_monotonic > 0:
            age_ms = (time.monotonic() - self._last_frame_monotonic) * 1000.0
        stream_state = "ONLINE" if self._last_status == "OK" else ("OFFLINE" if self._last_status == "OFF" else "RECONNECT")
        self.info_label.setText(
            f"{cam_ip} | {stream_state} | {self._stream_type} | {resolution}"
        )
        self.status_label.setText(f"{self._status_text} | FPS {self._fps:0.1f} | {int(age_ms)}ms")


class FullscreenWindow(QWidget):
    def __init__(
        self,
        target_screen: QScreen | None = None,
        single_display_mode: bool = False,
        common_auth: dict | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.target_screen = target_screen
        self.single_display_mode = single_display_mode
        self.common_auth = common_auth or {}
        self.current_camera: dict | None = None
        self._last_image: QImage | None = None
        self._pending_switch = False

        self.setWindowTitle("拡大表示")
        self.setStyleSheet(
            """
            QWidget { background-color: #000000; color: #ffffff; }
            #controlBar { background-color: rgba(20, 20, 20, 180); }
            QPushButton { background-color: #2e2e2e; color: #f0f0f0; border: 1px solid #444; padding: 8px 14px; }
            QPushButton:hover { background-color: #3b3b3b; }
            """
        )

        self.video_widget = ZoomPanVideoWidget()
        self.switching_label = QLabel("")
        self.switching_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.switching_label.setStyleSheet("color: #7fe8ff; font-size: 18px; font-weight: bold;")

        self.open_settings_button = QPushButton("設定画面を開く")
        self.close_button = QPushButton("閉じる")
        self.open_settings_button.clicked.connect(self._open_settings)
        self.close_button.clicked.connect(self.close)

        control_bar = QWidget()
        control_bar.setObjectName("controlBar")
        control_layout = QHBoxLayout(control_bar)
        control_layout.setContentsMargins(8, 8, 8, 8)
        control_layout.addStretch(1)
        control_layout.addWidget(self.open_settings_button)
        control_layout.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.video_widget, 1)
        layout.addWidget(self.switching_label)
        layout.addWidget(control_bar, 0)

        self.player = StreamPlayer(self)
        self.player.frame_ready.connect(self._update_frame)

    def set_camera(self, camera_config: dict) -> None:
        self.current_camera = camera_config
        self.setWindowTitle(f"拡大表示 - {camera_config.get('name', camera_config.get('id', ''))}")
        self._pending_switch = True
        self.switching_label.setText("切替中...")

        if not camera_config.get("enabled", True):
            self.player.stop()
            self.video_widget.reset_view()
            self.switching_label.setText("OFFLINE")
            return

        rtsp_main = camera_config.get("rtsp_main")
        if not rtsp_main:
            QMessageBox.warning(self, "設定不足", "このカメラにはメインストリームURLがありません。")
            return

        self.player.start(rtsp_main, target_fps=5.0, camera_label=str(camera_config.get("id", "cam?")))

    def show_on_target(self) -> None:
        if not self.target_screen:
            self.showMaximized()
            return

        screen_geo = self.target_screen.geometry()

        if self.single_display_mode:
            self.move(screen_geo.topLeft())
            self.showMaximized()
            return

        self.move(screen_geo.topLeft())
        self.showMaximized()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.player.stop()
        super().closeEvent(event)

    def stop_stream(self) -> None:
        self.player.stop()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)

    def _update_frame(self, image: QImage) -> None:
        self._last_image = image
        if self._pending_switch:
            self.switching_label.setText("")
            self._pending_switch = False
        self.video_widget.set_image(image)

    def _open_settings(self) -> None:
        if not self.current_camera:
            return
        open_camera_settings_with_login(self.current_camera, self.common_auth, parent=self)


class MainWindow(QMainWindow):
    def __init__(self, config: dict, fullscreen_screen: QScreen | None, single_display_mode: bool):
        super().__init__()
        self.config = config
        self.cameras = config.get("cameras", [])
        self.groups = config.get("groups", {})
        self.fullscreen_screen = fullscreen_screen
        self.single_display_mode = single_display_mode
        self.single_instance = bool(config.get("app", {}).get("fullscreen_single_instance", True))

        self.fullscreen_window: FullscreenWindow | None = None
        self.tiles_by_camera_id: dict[str, CameraTile] = {}
        self.mode_buttons: dict[str, QPushButton] = {}
        self.current_mode = ""
        self._switch_in_progress = False
        self._pending_mode: str | None = None
        self._mode_switch_token = 0
        self._last_mode_request_at: dict[str, float] = {}
        self._switch_debounce_seconds = 0.45
        self._switch_cooldown_timer = QTimer(self)
        self._switch_cooldown_timer.setSingleShot(True)
        self._switch_cooldown_timer.timeout.connect(self._process_pending_mode)
        self._group_load_timeout_timer = QTimer(self)
        self._group_load_timeout_timer.setSingleShot(True)
        self._group_load_timeout_timer.timeout.connect(self._on_group_load_timeout)
        self._loading_camera_ids: set[str] = set()
        self._loaded_camera_ids: set[str] = set()
        self._current_visible_ids: list[str] = []
        self.loop_enabled = False
        self.loop_timer = QTimer(self)
        self.loop_timer.setInterval(60_000)
        self.loop_timer.timeout.connect(self._advance_group_loop)
        self.loop_button: QPushButton | None = None

        self.setWindowTitle("IPカメラ一覧表示")
        self.setStyleSheet(
            """
            QMainWindow { background-color: #03070d; }
            QPushButton { background-color: #0e1b27; color: #d8ebf1; border: 1px solid #21455c; padding: 8px; }
            QPushButton:checked { background-color: #0f4158; border: 1px solid #52cae8; }
            """
        )

        central = QWidget()
        self.setCentralWidget(central)
        self.root_layout = QVBoxLayout(central)
        self.root_layout.setContentsMargins(8, 8, 8, 8)
        self.root_layout.setSpacing(8)

        self.button_row = QHBoxLayout()
        self.button_row.setSpacing(6)
        self.root_layout.addLayout(self.button_row)
        self.switch_status_label = QLabel("")
        self.switch_status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.switch_status_label.setStyleSheet("color: #7fe8ff; font-size: 13px; font-weight: bold; padding-left: 3px;")
        self.root_layout.addWidget(self.switch_status_label)

        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setContentsMargins(2, 2, 2, 2)
        self.grid_layout.setSpacing(3)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidget(self.grid_widget)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.root_layout.addWidget(self.scroll_area, 1)

        self._build_mode_buttons()
        self._build_tiles()

        startup_mode = config.get("app", {}).get("startup_mode", "all")
        self.request_mode_switch(startup_mode)

    def _build_mode_buttons(self) -> None:
        modes = [("all", "18画面一括")] + [(f"group{i}", f"{i}グループ") for i in range(1, 7)]
        for mode_key, text in modes:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=False, m=mode_key: self._manual_switch_mode(m))
            self.mode_buttons[mode_key] = btn
            self.button_row.addWidget(btn)
        self.loop_button = QPushButton("ループ表示: OFF")
        self.loop_button.setCheckable(True)
        self.loop_button.clicked.connect(self._toggle_loop_mode)
        self.button_row.addWidget(self.loop_button)
        self.button_row.addStretch(1)

    def _build_tiles(self) -> None:
        for cam in self.cameras:
            tile = CameraTile(cam)
            tile.clicked.connect(self.open_fullscreen)
            tile.first_frame_ready.connect(self._on_tile_first_frame)
            self.tiles_by_camera_id[cam["id"]] = tile

    def request_mode_switch(self, mode: str, *, debounce: bool = False) -> None:
        logger.info("[VIEW] switch requested: %s", mode)
        if mode not in self.mode_buttons:
            logger.warning("Unknown mode request: %s", mode)
            return

        now = time.monotonic()
        if debounce:
            last_at = self._last_mode_request_at.get(mode, 0.0)
            if (now - last_at) < self._switch_debounce_seconds:
                logger.info("[VIEW] switch debounced: %s", mode)
                if self._switch_in_progress:
                    self._pending_mode = mode
                return
            self._last_mode_request_at[mode] = now

        if mode == self.current_mode and not self._switch_in_progress:
            logger.info("[VIEW] switch ignored(same): %s", mode)
            return

        if self._switch_in_progress:
            self._pending_mode = mode
            logger.info("[VIEW] switch deferred: %s", mode)
            return

        self._start_mode_switch(mode)

    def _start_mode_switch(self, mode: str) -> None:
        self._switch_in_progress = True
        self._mode_switch_token += 1
        token = self._mode_switch_token
        self._set_mode_buttons_enabled(False)
        self.switch_status_label.setText("画面切替中...")
        logger.info("[VIEW] switch started: %s", mode)

        self.current_mode = mode
        for key, button in self.mode_buttons.items():
            button.setChecked(key == mode)

        QTimer.singleShot(0, lambda t=token, m=mode: self._switch_stage_stop_current(t, m))

    def _switch_stage_stop_current(self, token: int, mode: str) -> None:
        if token != self._mode_switch_token:
            return
        try:
            self.switch_status_label.setText("画面切替中... カメラ停止")
            self._clear_current_group()
            self._stop_fullscreen_stream()
        except Exception as exc:  # noqa: BLE001
            self._handle_switch_failure(mode, exc)
            return
        QTimer.singleShot(50, lambda t=token, m=mode: self._switch_stage_build_layout(t, m))

    def _switch_stage_build_layout(self, token: int, mode: str) -> None:
        if token != self._mode_switch_token:
            return
        try:
            self.switch_status_label.setText("画面切替中... レイアウト更新")
            visible_ids = self._visible_camera_ids(mode)
            self._current_visible_ids = visible_ids
            self._loading_camera_ids = {
                cam_id
                for cam_id in visible_ids
                if self.tiles_by_camera_id.get(cam_id) and self.tiles_by_camera_id[cam_id].camera_config.get("enabled", True)
            }
            self._loaded_camera_ids = set()
            self._layout_tiles(visible_ids)
        except Exception as exc:  # noqa: BLE001
            self._handle_switch_failure(mode, exc)
            return
        QTimer.singleShot(0, lambda t=token: self._switch_stage_start_streams(t))

    def _switch_stage_start_streams(self, token: int) -> None:
        if token != self._mode_switch_token:
            return
        try:
            self.switch_status_label.setText("カメラ再接続中...")
            logger.info("starting target camera streams: mode=%s count=%d", self.current_mode, len(self._current_visible_ids))
            self._update_streams(self._current_visible_ids)
            self._group_load_timeout_timer.start(15_000)
            if not self._loading_camera_ids:
                self._finish_mode_switch()
        except Exception as exc:  # noqa: BLE001
            self._handle_switch_failure(self.current_mode, exc)

    def _handle_switch_failure(self, mode: str, exc: Exception) -> None:
        logger.exception("[VIEW] switch failed: %s error=%s", mode, exc)
        self._switch_in_progress = False
        self._group_load_timeout_timer.stop()
        self.switch_status_label.setText("")
        self._set_mode_buttons_enabled(True)

    def _clear_current_group(self) -> None:
        stop_count = 0
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            camera_id = getattr(widget, "camera_config", {}).get("id", "unknown")
            if isinstance(widget, CameraTile):
                logger.info("stopping tile camera_id=%s", camera_id)
                widget.stop_stream()
                logger.info("stream stopped camera_id=%s", camera_id)
                stop_count += 1
            widget.setParent(None)
        logger.info("clearing old group completed: stopped_count=%d", stop_count)

    def _stop_fullscreen_stream(self) -> None:
        if self.fullscreen_window is None:
            return
        logger.info("stopping fullscreen stream before mode switch")
        self.fullscreen_window.stop_stream()

    def _visible_camera_ids(self, mode: str) -> list[str]:
        if mode == "all":
            return [c["id"] for c in self.cameras][:18]

        group_no = mode.replace("group", "")
        return self.groups.get(group_no, [])

    def _layout_tiles(self, visible_ids: list[str]) -> None:
        group_mode = self.current_mode.startswith("group")
        columns = 1 if group_mode else 3
        if group_mode:
            self.grid_layout.setContentsMargins(0, 0, 0, 0)
            self.grid_layout.setSpacing(0)
        else:
            self.grid_layout.setContentsMargins(2, 2, 2, 2)
            self.grid_layout.setSpacing(3)

        for index, cam_id in enumerate(visible_ids):
            tile = self.tiles_by_camera_id.get(cam_id)
            if not tile:
                continue
            tile.set_display_mode(group_mode=group_mode)
            row = index // columns
            col = index % columns
            self.grid_layout.addWidget(tile, row, col)

    def _manual_switch_mode(self, mode: str) -> None:
        if mode != self.current_mode and self.loop_enabled:
            self._stop_loop()
        self.request_mode_switch(mode, debounce=True)

    def _toggle_loop_mode(self, checked: bool) -> None:
        if checked:
            self.loop_enabled = True
            logger.info("loop mode ON")
            self.loop_button.setText("ループ表示: ON")
            self.loop_button.setStyleSheet("background-color: #145d7b; border: 1px solid #7fe8ff;")
            if self.current_mode == "all":
                self.request_mode_switch("group1")
            self.loop_timer.start()
        else:
            self._stop_loop()

    def _stop_loop(self) -> None:
        self.loop_enabled = False
        self.loop_timer.stop()
        logger.info("loop mode OFF")
        if self.loop_button is not None:
            self.loop_button.blockSignals(True)
            self.loop_button.setChecked(False)
            self.loop_button.blockSignals(False)
            self.loop_button.setText("ループ表示: OFF")
            self.loop_button.setStyleSheet("")

    def _advance_group_loop(self) -> None:
        group_modes = [f"group{i}" for i in range(1, 7)]
        if self.current_mode not in group_modes:
            self.request_mode_switch("group1")
            return
        current_idx = group_modes.index(self.current_mode)
        next_mode = group_modes[(current_idx + 1) % len(group_modes)]
        self.request_mode_switch(next_mode)

    def _update_streams(self, visible_ids: list[str]) -> None:
        visible = set(visible_ids)
        for cam_id, tile in self.tiles_by_camera_id.items():
            if cam_id in visible:
                tile.start_stream()
            else:
                tile.stop_stream()
        logger.info("stream update complete: start_count=%d stop_count=%d", len(visible), len(self.tiles_by_camera_id) - len(visible))

    def _finish_mode_switch(self) -> None:
        if not self._switch_in_progress:
            return
        try:
            logger.info(
                "group load complete: mode=%s loaded=%d expected=%d",
                self.current_mode,
                len(self._loaded_camera_ids),
                len(self._current_visible_ids),
            )
            logger.info("[VIEW] switch finished: %s", self.current_mode)
        finally:
            self._switch_in_progress = False
            self._group_load_timeout_timer.stop()
            self.switch_status_label.setText("")
            self._set_mode_buttons_enabled(True)
        if self._pending_mode and self._pending_mode != self.current_mode:
            logger.info("[VIEW] switch retry deferred request: %s", self._pending_mode)
            self._switch_cooldown_timer.start(350)
        elif self._pending_mode == self.current_mode:
            self._pending_mode = None

    def _process_pending_mode(self) -> None:
        if not self._pending_mode:
            return
        next_mode = self._pending_mode
        self._pending_mode = None
        logger.info("processing pending mode: %s", next_mode)
        self.request_mode_switch(next_mode)

    def _set_mode_buttons_enabled(self, enabled: bool) -> None:
        for button in self.mode_buttons.values():
            button.setEnabled(enabled)
            button.setStyleSheet("" if enabled else "background-color: #1a2733; color: #6f8798; border: 1px solid #2a3f50;")
        if self.loop_button is not None:
            self.loop_button.setEnabled(enabled)
            if not enabled:
                self.loop_button.setStyleSheet("background-color: #1f2d39; color: #7f9cad;")
            elif self.loop_enabled:
                self.loop_button.setStyleSheet("background-color: #145d7b; border: 1px solid #7fe8ff;")
            else:
                self.loop_button.setStyleSheet("")

    def _on_tile_first_frame(self, camera_id: str) -> None:
        if not self._switch_in_progress:
            return
        if camera_id not in self._loading_camera_ids:
            return
        self._loaded_camera_ids.add(camera_id)
        self._loading_camera_ids.discard(camera_id)
        logger.info(
            "first frame arrived: mode=%s camera_id=%s remaining=%d",
            self.current_mode,
            camera_id,
            len(self._loading_camera_ids),
        )
        if not self._loading_camera_ids:
            self._finish_mode_switch()

    def _on_group_load_timeout(self) -> None:
        if not self._switch_in_progress:
            return
        logger.warning("group load timeout: mode=%s pending_unloaded=%s", self.current_mode, sorted(self._loading_camera_ids))
        self._finish_mode_switch()

    def open_fullscreen(self, camera_config: dict) -> None:
        if self.single_instance and self.fullscreen_window is not None:
            self.fullscreen_window.set_camera(camera_config)
            self.fullscreen_window.show_on_target()
            self.fullscreen_window.raise_()
            self.fullscreen_window.activateWindow()
            return

        self.fullscreen_window = FullscreenWindow(
            target_screen=self.fullscreen_screen,
            single_display_mode=self.single_display_mode,
            common_auth=self.config.get("common_auth", {}),
        )
        self.fullscreen_window.destroyed.connect(self._on_fullscreen_destroyed)
        self.fullscreen_window.set_camera(camera_config)
        self.fullscreen_window.show_on_target()

    def _on_fullscreen_destroyed(self) -> None:
        self.fullscreen_window = None

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.loop_timer.stop()
        for tile in self.tiles_by_camera_id.values():
            tile.stop_stream()
        if self.fullscreen_window:
            self.fullscreen_window.close()
        super().closeEvent(event)


def load_config(config_path: Path) -> dict:
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"config 読み込み失敗: {config_path}\n{exc}") from exc

    if "cameras" not in data or not isinstance(data["cameras"], list):
        raise RuntimeError("config.json の cameras 定義が不正です")
    if "groups" not in data or not isinstance(data["groups"], dict):
        raise RuntimeError("config.json の groups 定義が不正です")

    return data


def main() -> int:
    app = QApplication(sys.argv)

    config_path = Path(__file__).resolve().parent / "config.json"
    try:
        config = load_config(config_path)
    except RuntimeError as exc:
        QMessageBox.critical(None, "設定エラー", str(exc))
        return 1

    for camera in config.get("cameras", []):
        camera_id = camera.get("id", "unknown")
        enabled = camera.get("enabled", True)
        if enabled:
            logger.info("Camera %s: ENABLED", camera_id)
        else:
            logger.info("Camera %s: OFF (未設置)", camera_id)

    screens = QGuiApplication.screens()

    if not screens:
        QMessageBox.critical(None, "ディスプレイエラー", "利用可能なディスプレイが見つかりません")
        return 1

    portrait_screens: list[QScreen] = []
    landscape_screens: list[QScreen] = []

    for index, screen in enumerate(screens):
        geo = screen.geometry()
        orientation = "portrait" if geo.height() > geo.width() else "landscape"
        if orientation == "portrait":
            portrait_screens.append(screen)
        else:
            landscape_screens.append(screen)
        logger.info(
            "Screen[%d]: name=%s width=%d height=%d orientation=%s",
            index,
            screen.name(),
            geo.width(),
            geo.height(),
            orientation,
        )

    # 優先: 縦型=一括表示, 横型=拡大表示 (見つからない場合のみフォールバック)
    grid_screen = portrait_screens[0] if portrait_screens else screens[0]
    fullscreen_screen = (
        landscape_screens[0]
        if landscape_screens
        else (screens[1] if len(screens) > 1 else screens[0])
    )
    single_display_mode = len(screens) < 2

    main_window = MainWindow(
        config=config,
        fullscreen_screen=fullscreen_screen,
        single_display_mode=single_display_mode,
    )

    main_window.move(grid_screen.geometry().topLeft())
    main_window.showMaximized()

    logger.info(
        "Display assignment result: grid_screen=%s fullscreen_screen=%s single_display_mode=%s",
        grid_screen.name(),
        fullscreen_screen.name(),
        single_display_mode,
    )
    logger.info("Application started")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
