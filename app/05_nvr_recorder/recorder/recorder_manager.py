"""Coordinator for up to 20 camera recorder workers."""
from __future__ import annotations

import logging
from pathlib import Path

from config.config_store import CameraConfig
from recorder.camera_recorder import CameraRecorder
from recorder.ffmpeg_runner import FFmpegRunner
from status.status_writer import StatusWriter
from commands.command_processor import CommandProcessor
from utils.disk_cleanup import cleanup_by_retention, cleanup_for_free_space, quarantine_old_partials


class RecorderManager:
    def __init__(self, cameras: list[CameraConfig], settings: dict, logger: logging.Logger) -> None:
        self.cameras = cameras[:20]
        self.settings = settings
        self.logger = logger.getChild("manager")
        self.runner = FFmpegRunner(settings.get("ffmpeg_path", "ffmpeg"), settings.get("ffprobe_path", "ffprobe"))
        self.recorders = {c.id: CameraRecorder(c, settings, self.runner, logger) for c in self.cameras}
        self._running = False
        self.status_writer = StatusWriter(settings, self.camera_statuses, self.system_status)
        self.command_processor = CommandProcessor(
            settings["commands_dir"], self.handle_command, logger, int(settings.get("command_interval_seconds", 3))
        )

    def prepare_directories(self) -> None:
        for key in ("temp_dir", "archive_dir", "config_dir", "status_dir", "commands_dir", "logs_dir"):
            Path(self.settings[key]).mkdir(parents=True, exist_ok=True)
        for sub in ("pending", "processed", "failed"):
            (Path(self.settings["commands_dir"]) / sub).mkdir(parents=True, exist_ok=True)
        quarantine_old_partials(self.settings["temp_dir"], self.logger)

    def start_services(self) -> None:
        self.prepare_directories()
        self.status_writer.start()
        self.command_processor.start()
        self.logger.info("アプリ起動")

    def stop_services(self) -> None:
        self.stop_all()
        self.command_processor.stop()
        self.status_writer.stop()
        self.logger.info("アプリ終了")

    def start_all(self) -> None:
        self._running = True
        for recorder in self.recorders.values():
            recorder.start()
        self.logger.info("全カメラ録画開始")

    def stop_all(self) -> None:
        for recorder in self.recorders.values():
            recorder.stop()
        self._running = False
        self.logger.info("全カメラ録画停止")

    def start_camera(self, camera_id: int) -> None:
        self.recorders[camera_id].start()
        self._running = True

    def stop_camera(self, camera_id: int) -> None:
        self.recorders[camera_id].stop()

    def split_all(self, reason: str = "手動区切り") -> None:
        self.logger.info("MP4区切りボタン実行: %s", reason)
        for recorder in self.recorders.values():
            recorder.split_now(reason)

    def test_camera(self, camera_id: int) -> tuple[bool, str]:
        return self.recorders[camera_id].test_connection()

    def cleanup(self) -> None:
        cleanup_by_retention(self.settings["archive_dir"], self.cameras, self.logger)
        cleanup_for_free_space(self.settings["archive_dir"], float(self.settings.get("min_free_gb", 50)), self.logger)

    def camera_statuses(self) -> list[dict]:
        return [self.recorders[camera.id].snapshot() for camera in self.cameras]

    def system_status(self) -> str:
        return "running" if self._running else "stopped"

    def handle_command(self, command: dict) -> dict:
        command_type = command.get("type")
        params = command.get("params") or {}
        if command_type == "split_all_mp4":
            self.split_all(params.get("reason", "commands"))
        elif command_type == "start_all":
            self.start_all()
        elif command_type == "stop_all":
            self.stop_all()
        elif command_type == "start_camera":
            self.start_camera(int(params["camera_id"]))
        elif command_type == "stop_camera":
            self.stop_camera(int(params["camera_id"]))
        else:
            raise ValueError(f"未対応コマンド: {command_type}")
        return {"type": command_type}
