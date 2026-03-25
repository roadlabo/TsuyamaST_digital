from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent, QImage, QPixmap, QScreen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from browser_launcher import open_camera_settings
from stream_player import StreamPlayer


class FullscreenWindow(QWidget):
    def __init__(self, target_screen: QScreen | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.target_screen = target_screen
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

        rtsp_main = camera_config.get("rtsp_main")
        if not rtsp_main:
            QMessageBox.warning(self, "設定不足", "このカメラにはメインストリームURLがありません。")
            return

        self.player.start(rtsp_main, target_fps=12.0)

    def show_on_target(self, fullscreen: bool = True) -> None:
        if self.target_screen:
            self.setGeometry(self.target_screen.geometry())
        if fullscreen:
            self.showFullScreen()
        else:
            self.show()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.player.stop()
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
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
        open_camera_settings(self.current_camera.get("web_url", ""), parent=self)
