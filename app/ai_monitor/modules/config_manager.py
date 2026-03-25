from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SYSTEM_CONFIG: dict[str, Any] = {
    "model_path": "yolo11n.pt",
    "device_preference": "auto",
    "target_classes": ["car", "bus", "truck", "motorcycle"],
    "metrics_save_interval_sec": 5,
    "ui_refresh_interval_ms": 500,
    "ai_status_json_path": "app/config/ai_status.json",
    "status_update_interval_sec": 3,
    "output_root": "app/ai_monitor/data",
}

DEFAULT_CAMERA_SETTINGS: dict[str, Any] = {
    "cameras": [
        {
            "camera_id": 1,
            "camera_name": "Camera1",
            "stream_url": "0",
            "enabled": True,
            "direction": "LtoR",
            "line_points": [[100, 300], [1000, 300]],
            "exclude_polygon": [],
            "congestion_threshold": 60,
            "long_stay_minutes": 15,
            "long_stay_trigger_count": 1,
            "stay_zone_polygon": [],
            "reconnect_sec": 3,
        },
        {
            "camera_id": 2,
            "camera_name": "Camera2",
            "stream_url": "1",
            "enabled": True,
            "direction": "LtoR",
            "line_points": [[100, 300], [1000, 300]],
            "exclude_polygon": [],
            "congestion_threshold": 60,
            "long_stay_minutes": 15,
            "long_stay_trigger_count": 1,
            "stay_zone_polygon": [],
            "reconnect_sec": 3,
        },
        {
            "camera_id": 3,
            "camera_name": "Camera3",
            "stream_url": "2",
            "enabled": True,
            "direction": "LtoR",
            "line_points": [[100, 300], [1000, 300]],
            "exclude_polygon": [],
            "congestion_threshold": 65,
            "long_stay_minutes": 15,
            "long_stay_trigger_count": 1,
            "stay_zone_polygon": [],
            "reconnect_sec": 3,
        },
    ]
}


@dataclass
class AppConfig:
    root_dir: Path
    system_config_path: Path
    camera_settings_path: Path
    system: dict[str, Any]
    cameras: list[dict[str, Any]]


class ConfigManager:
    """config.py の設定管理スタイルを参考に、JSON管理を一元化する。"""

    def __init__(self, root_dir: Path):
        self.root_dir = root_dir
        self.config_dir = root_dir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self.system_config_path = self.config_dir / "system_config.json"
        self.camera_settings_path = self.config_dir / "camera_settings.json"

    def ensure_defaults(self) -> None:
        if not self.system_config_path.exists():
            self._atomic_write_json(self.system_config_path, DEFAULT_SYSTEM_CONFIG)
        if not self.camera_settings_path.exists():
            self._atomic_write_json(self.camera_settings_path, DEFAULT_CAMERA_SETTINGS)

    def load(self) -> AppConfig:
        self.ensure_defaults()
        system = json.loads(self.system_config_path.read_text(encoding="utf-8"))
        camera_dict = json.loads(self.camera_settings_path.read_text(encoding="utf-8"))
        cameras = camera_dict.get("cameras", [])
        return AppConfig(
            root_dir=self.root_dir,
            system_config_path=self.system_config_path,
            camera_settings_path=self.camera_settings_path,
            system=system,
            cameras=cameras,
        )

    def save_camera_settings(self, cameras: list[dict[str, Any]]) -> None:
        self._atomic_write_json(self.camera_settings_path, {"cameras": cameras})

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
