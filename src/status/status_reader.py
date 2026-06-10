"""Robust read-only status/config reader for office PC."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.atomic_file import read_json


class StatusReader:
    def __init__(self, status_dir: str | Path, config_dir: str | Path | None = None) -> None:
        self.status_dir = Path(status_dir)
        self.config_dir = Path(config_dir) if config_dir else None
        self.last_good_status: dict[str, Any] | None = None
        self.last_error = ""

    def read_status(self) -> dict[str, Any] | None:
        try:
            data = read_json(self.status_dir / "system_status.json", None)
            if data is not None:
                self.last_good_status = data
                self.last_error = ""
            return self.last_good_status
        except Exception as exc:
            self.last_error = f"状態JSON読込エラー: {exc}"
            return self.last_good_status

    def read_cameras_config(self) -> list[dict[str, Any]]:
        if self.config_dir is None:
            return []
        try:
            data = read_json(self.config_dir / "cameras.json", []) or []
            return data.get("cameras", []) if isinstance(data, dict) else data
        except Exception as exc:
            self.last_error = f"設定JSON読込エラー: {exc}"
            return []
