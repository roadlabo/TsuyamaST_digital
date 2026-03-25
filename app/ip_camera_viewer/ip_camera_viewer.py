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
from PyQt6.QtCore import QMutex, QObject, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QGuiApplication, QImage, QMouseEvent, QPixmap, QResizeEvent, QScreen
from PyQt6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
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

    def __init__(self, rtsp_url: str, target_fps: float = 5.0, reconnect_seconds: float = 4.0, parent: QObject | None = None):
        super().__init__(parent)
        self.rtsp_url = rtsp_url
        self.target_fps = target_fps
        self.reconnect_seconds = reconnect_seconds
        self._running = False
        self._mutex = QMutex()
        self._cap: cv2.VideoCapture | None = None

    def stop(self) -> None:
        self._mutex.lock()
        self._running = False
        cap = self._cap
        self._mutex.unlock()
        if cap is not None:
            cap.release()

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
            self._mutex.lock()
            self._cap = cap
            self._mutex.unlock()
            if not cap.isOpened():
                self.status_changed.emit("ERROR")
                logger.warning("Failed to connect stream: %s", self.rtsp_url)
                cap.release()
                self._mutex.lock()
                self._cap = None
                self._mutex.unlock()
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
            self._mutex.lock()
            self._cap = None
            self._mutex.unlock()
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
        self._stopping = False

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
        if self._stopping:
            return
        worker = self._worker
        if worker is None:
            return

        self._stopping = True
        try:
            worker.stop()
            worker.quit()
            finished = worker.wait(3000)
            if not finished:
                logger.warning("Stream worker did not finish in timeout. force terminate.")
                worker.terminate()
                worker.wait(1000)
            worker.deleteLater()
            self._worker = None
        finally:
            self._stopping = False


class CameraTile(QWidget):
    clicked = pyqtSignal(dict)

    def __init__(self, camera_config: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.camera_config = camera_config
        self._last_image: QImage | None = None
        self.setFixedSize(320, 240)

        self.setStyleSheet(
            """
            QWidget { background-color: #111111; border: 1px solid #2a2a2a; color: #e0e0e0; }
            QLabel#title { font-size: 14px; font-weight: bold; padding: 4px 6px; }
            QLabel#status { font-size: 12px; color: #ff8888; padding: 2px 6px; }
            """
        )

        self.title_label = QLabel(camera_config.get("name", camera_config.get("id", "Unknown")))
        self.title_label.setObjectName("title")

        self.video_label = QLabel("読み込み中...")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setFixedSize(320, 240)
        self.video_label.setScaledContents(False)

        self.status_label = QLabel("接続待機")
        self.status_label.setObjectName("status")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        layout.addWidget(self.title_label)
        layout.addWidget(self.video_label, 1)
        layout.addWidget(self.status_label)

        self.stream_player = StreamPlayer(self)
        self.stream_player.frame_ready.connect(self._update_frame)
        self.stream_player.status_changed.connect(self._update_status)

    def start_stream(self) -> None:
        if not self.camera_config.get("enabled", True):
            self.show_offline()
            self._update_status("OFF")
            return

        rtsp_sub = self.camera_config.get("rtsp_sub")
        if not rtsp_sub:
            self._update_status("ERROR")
            self.video_label.setText("サブストリーム未設定")
            return
        self.stream_player.start(rtsp_sub, target_fps=5.0)

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
        if self._last_image:
            self._set_pixmap(self._last_image)

    def _update_frame(self, image: QImage) -> None:
        self._last_image = image
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
        if status == "OK":
            self.status_label.setText("OK")
            self.status_label.setStyleSheet("color: #8fff8f;")
            return

        if status == "OFF":
            self.status_label.setText("OFF / 未接続")
            self.status_label.setStyleSheet("color: #aaaaaa;")
        else:
            self.status_label.setText("ERROR / 接続失敗")
            self.status_label.setStyleSheet("color: #ff8888;")


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

        self.setWindowTitle("拡大表示")
        self.setStyleSheet(
            """
            QWidget { background-color: #000000; color: #ffffff; }
            #controlBar { background-color: rgba(20, 20, 20, 180); }
            QPushButton { background-color: #2e2e2e; color: #f0f0f0; border: 1px solid #444; padding: 8px 14px; }
            QPushButton:hover { background-color: #3b3b3b; }
            """
        )

        self.video_label = QLabel("カメラを選択してください")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

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
        layout.addWidget(self.video_label, 1)
        layout.addWidget(control_bar, 0)

        self.player = StreamPlayer(self)
        self.player.frame_ready.connect(self._update_frame)

    def set_camera(self, camera_config: dict) -> None:
        self.current_camera = camera_config
        self.setWindowTitle(f"拡大表示 - {camera_config.get('name', camera_config.get('id', ''))}")

        if not camera_config.get("enabled", True):
            self.player.stop()
            self.video_label.setPixmap(QPixmap())
            self.video_label.setText("未接続\n(準備中)")
            return

        rtsp_main = camera_config.get("rtsp_main")
        if not rtsp_main:
            QMessageBox.warning(self, "設定不足", "このカメラにはメインストリームURLがありません。")
            return

        self.player.start(rtsp_main, target_fps=12.0)

    def show_on_target(self) -> None:
        if not self.target_screen:
            self.show()
            return

        screen_geo = self.target_screen.geometry()

        if self.single_display_mode:
            w = int(screen_geo.width() * 0.72)
            h = int(screen_geo.height() * 0.72)
            x = screen_geo.x() + (screen_geo.width() - w) // 2
            y = screen_geo.y() + (screen_geo.height() - h) // 2
            self.setGeometry(x, y, w, h)
            self.show()
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
        if self._last_image:
            self._set_pixmap(self._last_image)

    def _update_frame(self, image: QImage) -> None:
        self._last_image = image
        self._set_pixmap(image)

    def _set_pixmap(self, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image)
        self.video_label.setPixmap(
            pixmap.scaled(self.video_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        )

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
        self.current_mode = "all"
        self._switching_group = False

        self.setWindowTitle("IPカメラ一覧表示")
        self.setStyleSheet(
            """
            QMainWindow { background-color: #0b0b0b; }
            QPushButton { background-color: #242424; color: #f0f0f0; border: 1px solid #3f3f3f; padding: 8px; }
            QPushButton:checked { background-color: #1f4d7a; border: 1px solid #3f87c3; }
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

        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setContentsMargins(5, 5, 5, 5)
        self.grid_layout.setSpacing(5)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidget(self.grid_widget)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.root_layout.addWidget(self.scroll_area, 1)

        self._build_mode_buttons()
        self._build_tiles()

        startup_mode = config.get("app", {}).get("startup_mode", "all")
        self.switch_mode(startup_mode)

    def _build_mode_buttons(self) -> None:
        modes = [("all", "18画面一括")] + [(f"group{i}", f"{i}グループ") for i in range(1, 7)]
        for mode_key, text in modes:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=False, m=mode_key: self.switch_mode(m))
            self.mode_buttons[mode_key] = btn
            self.button_row.addWidget(btn)
        self.button_row.addStretch(1)

    def _build_tiles(self) -> None:
        for cam in self.cameras:
            tile = CameraTile(cam)
            tile.clicked.connect(self.open_fullscreen)
            self.tiles_by_camera_id[cam["id"]] = tile

    def switch_mode(self, mode: str) -> None:
        if self._switching_group:
            logger.info("group switch skipped (already switching): requested_mode=%s", mode)
            return

        self._switching_group = True
        logger.info("group switch start: from=%s to=%s", self.current_mode, mode)
        try:
            self._switch_mode_internal(mode)
        finally:
            self._switching_group = False
            logger.info("group switch completed: current=%s", self.current_mode)

    def _switch_mode_internal(self, mode: str) -> None:
        if mode not in self.mode_buttons:
            logger.warning("Unknown startup/mode '%s'. fallback to all", mode)
            mode = "all"

        logger.info("loading new group mode=%s", mode)
        self.current_mode = mode
        for key, button in self.mode_buttons.items():
            button.setChecked(key == mode)

        self._clear_current_group()

        visible_ids = self._visible_camera_ids(mode)
        self._stop_fullscreen_stream()
        self._layout_tiles(visible_ids)
        self._update_streams(visible_ids)

    def _clear_current_group(self) -> None:
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
            widget.setParent(None)
        logger.info("clearing old group completed")

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
        columns = 3

        for index, cam_id in enumerate(visible_ids):
            tile = self.tiles_by_camera_id.get(cam_id)
            if not tile:
                continue
            row = index // columns
            col = index % columns
            self.grid_layout.addWidget(tile, row, col)

    def _update_streams(self, visible_ids: list[str]) -> None:
        visible = set(visible_ids)
        for cam_id, tile in self.tiles_by_camera_id.items():
            if cam_id in visible:
                tile.start_stream()
            else:
                tile.stop_stream()

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

    vertical_screen = None
    horizontal_screen = None

    for screen in screens:
        geo = screen.geometry()
        if geo.height() > geo.width():
            vertical_screen = screen
        else:
            horizontal_screen = screen

    if not screens:
        QMessageBox.critical(None, "ディスプレイエラー", "利用可能なディスプレイが見つかりません")
        return 1

    if vertical_screen is None:
        vertical_screen = screens[0]
    if horizontal_screen is None:
        horizontal_screen = vertical_screen

    single_display_mode = len(screens) < 2 or vertical_screen is horizontal_screen

    main_window = MainWindow(
        config=config,
        fullscreen_screen=horizontal_screen,
        single_display_mode=single_display_mode,
    )

    if single_display_mode:
        geo = vertical_screen.geometry()
        w = int(geo.width() * 0.65)
        h = int(geo.height() * 0.85)
        main_window.setGeometry(geo.x(), geo.y(), w, h)
        main_window.show()
    else:
        main_window.move(vertical_screen.geometry().topLeft())
        main_window.showMaximized()

    logger.info(
        "Display assignment: vertical=%s horizontal=%s single_display_mode=%s",
        vertical_screen.name(),
        horizontal_screen.name(),
        single_display_mode,
    )
    logger.info("Application started")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
