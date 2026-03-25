from __future__ import annotations

import json
import sys
from pathlib import Path

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication, QMessageBox

from display_manager import assign_displays
from logger import setup_logger
from ui_main_window import MainWindow

logger = setup_logger(__name__)


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

    screens = QGuiApplication.screens()
    try:
        assignment = assign_displays(screens, config.get("displays", {}))
    except RuntimeError as exc:
        QMessageBox.critical(None, "ディスプレイエラー", str(exc))
        return 1

    window = MainWindow(
        config=config,
        fullscreen_screen=assignment.fullscreen_screen,
        single_display_mode=assignment.single_display_mode,
    )
    window.setGeometry(assignment.grid_screen.geometry())
    window.showFullScreen()

    logger.info("Application started")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
