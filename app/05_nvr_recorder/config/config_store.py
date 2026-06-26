"""JSON configuration loader/saver for cameras and application settings."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from utils.atomic_file import atomic_write_json, read_json

MAX_CAMERAS = 20
DEFAULT_CONFIG_DIR = "D:/NVR/config"


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


def build_dir_settings(base_dir: str | Path) -> dict[str, str]:
    """Build standard runtime folders from one recording base directory."""
    base = Path(base_dir)
    normalized = base.as_posix()
    return {
        "base_dir": normalized,
        "temp_dir": (base / "temp").as_posix(),
        "archive_dir": (base / "archive").as_posix(),
        "status_dir": (base / "status").as_posix(),
        "commands_dir": (base / "commands").as_posix(),
        "logs_dir": (base / "logs").as_posix(),
        "quarantine_dir": (base / "quarantine").as_posix(),
    }


DEFAULT_SETTINGS: dict[str, Any] = {
    **build_dir_settings("D:/NVR"),
    "config_dir": DEFAULT_CONFIG_DIR,
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "min_free_gb": 50,
    "status_interval_seconds": 5,
    "command_interval_seconds": 3,
}


def normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Merge settings and keep runtime folders aligned with base_dir."""
    merged = DEFAULT_SETTINGS.copy()
    merged.update(settings)
    base_dir = str(merged.get("base_dir") or DEFAULT_SETTINGS["base_dir"])
    config_dir = str(merged.get("config_dir") or DEFAULT_CONFIG_DIR)
    merged.update(build_dir_settings(base_dir))
    merged["config_dir"] = config_dir
    return merged


def default_cameras() -> list[CameraConfig]:
    return [CameraConfig(id=i, save_subdir=f"cam{i:02d}") for i in range(1, MAX_CAMERAS + 1)]


class ConfigStore:
    def __init__(self, config_dir: str | Path = DEFAULT_CONFIG_DIR) -> None:
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
        return normalize_settings(data)

    def save_settings(self, settings: dict[str, Any]) -> None:
        atomic_write_json(self.settings_path, normalize_settings(settings))

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
