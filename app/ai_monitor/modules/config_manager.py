from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SYSTEM_CONFIG: dict[str, Any] = {
    "model_path": "yolo11n.pt",
    "device_preference": "auto",
    "metrics_save_interval_sec": 5,
    "ui_refresh_interval_ms": 500,
    "ai_status_json_path": "app/config/ai_status.json",
    "status_update_interval_sec": 3,
    "output_root": "app/ai_monitor/data",
    "display_update_interval_ms": 500,
    "graph_update_interval_sec": 10,
}

DEFAULT_CAMERA_SETTINGS: dict[str, Any] = {
    "cameras": [
        {
            "camera_id": 1,
            "camera_name": "Camera1",
            "stream_url": "0",
            "enabled": True,
            "line_start": [100, 300],
            "line_end": [1000, 300],
            "exclude_polygon": [],
            "congestion_threshold": 60,
            "long_stay_minutes": 15,
            "long_stay_trigger_count": 1,
            "stay_zone_polygon": [],
            "reconnect_sec": 3,
            "tracking_method": "bytetrack",
            "yolo_model": "yolo11n.pt",
            "confidence_threshold": 0.25,
            "iou_threshold": 0.5,
            "frame_skip": 1,
            "imgsz": 640,
            "target_classes": [2, 3, 5, 7],
            "bt_track_high_thresh": 0.3,
            "bt_track_low_thresh": 0.1,
            "bt_match_thresh": 0.8,
            "bt_track_buffer": 30,
            "crossing_judgment_pattern": "line_cross",
            "distance_threshold": 25.0,
            "congestion_calculation_interval": 10,
            "enable_congestion": True,
            "line_direction_mode": "line_vector",
        },
        {
            "camera_id": 2,
            "camera_name": "Camera2",
            "stream_url": "1",
            "enabled": True,
            "line_start": [100, 300],
            "line_end": [1000, 300],
            "exclude_polygon": [],
            "congestion_threshold": 60,
            "long_stay_minutes": 15,
            "long_stay_trigger_count": 1,
            "stay_zone_polygon": [],
            "reconnect_sec": 3,
            "tracking_method": "bytetrack",
            "yolo_model": "yolo11n.pt",
            "confidence_threshold": 0.25,
            "iou_threshold": 0.5,
            "frame_skip": 1,
            "imgsz": 640,
            "target_classes": [2, 3, 5, 7],
            "bt_track_high_thresh": 0.3,
            "bt_track_low_thresh": 0.1,
            "bt_match_thresh": 0.8,
            "bt_track_buffer": 30,
            "crossing_judgment_pattern": "line_cross",
            "distance_threshold": 25.0,
            "congestion_calculation_interval": 10,
            "enable_congestion": True,
            "line_direction_mode": "line_vector",
        },
        {
            "camera_id": 3,
            "camera_name": "Camera3",
            "stream_url": "2",
            "enabled": True,
            "line_start": [100, 300],
            "line_end": [1000, 300],
            "exclude_polygon": [],
            "congestion_threshold": 65,
            "long_stay_minutes": 15,
            "long_stay_trigger_count": 1,
            "stay_zone_polygon": [],
            "reconnect_sec": 3,
            "tracking_method": "bytetrack",
            "yolo_model": "yolo11n.pt",
            "confidence_threshold": 0.25,
            "iou_threshold": 0.5,
            "frame_skip": 1,
            "imgsz": 640,
            "target_classes": [2, 3, 5, 7],
            "bt_track_high_thresh": 0.3,
            "bt_track_low_thresh": 0.1,
            "bt_match_thresh": 0.8,
            "bt_track_buffer": 30,
            "crossing_judgment_pattern": "line_cross",
            "distance_threshold": 25.0,
            "congestion_calculation_interval": 10,
            "enable_congestion": True,
            "line_direction_mode": "line_vector",
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
        return AppConfig(self.root_dir, self.system_config_path, self.camera_settings_path, system, cameras)

    def save_camera_settings(self, cameras: list[dict[str, Any]]) -> None:
        self._atomic_write_json(self.camera_settings_path, {"cameras": cameras})

    def save_system_settings(self, system: dict[str, Any]) -> None:
        self._atomic_write_json(self.system_config_path, system)

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
