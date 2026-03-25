from __future__ import annotations

import webbrowser

from PyQt6.QtWidgets import QMessageBox, QWidget

from logger import setup_logger

logger = setup_logger(__name__)


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
