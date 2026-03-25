from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent, QScreen
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from camera_tile import CameraTile
from logger import setup_logger
from ui_fullscreen_window import FullscreenWindow

logger = setup_logger(__name__)


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
        if mode not in self.mode_buttons:
            logger.warning("Unknown startup/mode '%s'. fallback to all", mode)
            mode = "all"

        self.current_mode = mode
        for key, button in self.mode_buttons.items():
            button.setChecked(key == mode)

        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)

        visible_ids = self._visible_camera_ids(mode)
        self._layout_tiles(visible_ids)
        self._update_streams(visible_ids)

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
            self.fullscreen_window.show_on_target(fullscreen=not self.single_display_mode)
            self.fullscreen_window.raise_()
            self.fullscreen_window.activateWindow()
            return

        self.fullscreen_window = FullscreenWindow(target_screen=self.fullscreen_screen)
        self.fullscreen_window.destroyed.connect(self._on_fullscreen_destroyed)
        self.fullscreen_window.set_camera(camera_config)
        self.fullscreen_window.show_on_target(fullscreen=not self.single_display_mode)

    def _on_fullscreen_destroyed(self) -> None:
        self.fullscreen_window = None

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        for tile in self.tiles_by_camera_id.values():
            tile.stop_stream()
        if self.fullscreen_window:
            self.fullscreen_window.close()
        super().closeEvent(event)
