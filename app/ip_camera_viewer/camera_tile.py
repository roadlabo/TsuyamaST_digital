from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QImage, QMouseEvent, QPixmap
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from stream_player import StreamPlayer


class CameraTile(QWidget):
    clicked = pyqtSignal(dict)

    def __init__(self, camera_config: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.camera_config = camera_config
        self._last_image: QImage | None = None

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
        self.video_label.setMinimumHeight(140)

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

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.camera_config)
        super().mousePressEvent(event)

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
