"""JSON configuration loader/saver for cameras and application settings."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from utils.atomic_file import atomic_write_json, read_json

MAX_CAMERAS = 20


@dataclass
class CameraConfig:
    id: int
    name: str = ""
    enabled: bool = False
    rtsp_url: str = ""
    save_subdir: str = ""
    segment_minutes: int = 10
    retention_days: int = 30

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CameraConfig":
        cam_id = int(data.get("id", 0))
        return cls(
            id=cam_id,
            name=str(data.get("name", "")),
            enabled=bool(data.get("enabled", False)),
            rtsp_url=str(data.get("rtsp_url", "")),
            save_subdir=str(data.get("save_subdir") or f"cam{cam_id:02d}"),
            segment_minutes=max(1, int(data.get("segment_minutes", 10))),
            retention_days=max(1, int(data.get("retention_days", 30))),
        )


DEFAULT_SETTINGS: dict[str, Any] = {
    "base_dir": "D:/NVR",
    "temp_dir": "D:/NVR/temp",
    "archive_dir": "D:/NVR/archive",
    "config_dir": "D:/NVR/config",
    "status_dir": "D:/NVR/status",
    "commands_dir": "D:/NVR/commands",
    "logs_dir": "D:/NVR/logs",
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "min_free_gb": 50,
    "status_interval_seconds": 5,
    "command_interval_seconds": 3,
}


def default_cameras() -> list[CameraConfig]:
    return [CameraConfig(id=i, save_subdir=f"cam{i:02d}") for i in range(1, MAX_CAMERAS + 1)]


class ConfigStore:
    def __init__(self, config_dir: str | Path = "D:/NVR/config") -> None:
        self.config_dir = Path(config_dir)
        self.cameras_path = self.config_dir / "cameras.json"
        self.settings_path = self.config_dir / "app_settings.json"

    def ensure_defaults(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if not self.settings_path.exists():
            self.save_settings(DEFAULT_SETTINGS)
        if not self.cameras_path.exists():
            self.save_cameras(default_cameras())

    def load_settings(self) -> dict[str, Any]:
        data = read_json(self.settings_path, DEFAULT_SETTINGS.copy()) or DEFAULT_SETTINGS.copy()
        merged = DEFAULT_SETTINGS.copy()
        merged.update(data)
        return merged

    def save_settings(self, settings: dict[str, Any]) -> None:
        merged = DEFAULT_SETTINGS.copy()
        merged.update(settings)
        atomic_write_json(self.settings_path, merged)

    def load_cameras(self) -> list[CameraConfig]:
        raw = read_json(self.cameras_path, None)
        if raw is None:
            return default_cameras()
        if isinstance(raw, dict):
            raw = raw.get("cameras", [])
        cameras = [CameraConfig.from_dict(item) for item in raw[:MAX_CAMERAS]]
        known = {camera.id for camera in cameras}
        for cam_id in range(1, MAX_CAMERAS + 1):
            if cam_id not in known:
                cameras.append(CameraConfig(id=cam_id, save_subdir=f"cam{cam_id:02d}"))
        return sorted(cameras, key=lambda c: c.id)[:MAX_CAMERAS]

    def save_cameras(self, cameras: list[CameraConfig]) -> None:
        if len(cameras) > MAX_CAMERAS:
            raise ValueError("最大20台まで登録できます")
        atomic_write_json(self.cameras_path, [asdict(camera) for camera in cameras])
