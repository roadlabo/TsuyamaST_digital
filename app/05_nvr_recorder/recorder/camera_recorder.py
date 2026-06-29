"""Per-camera segmented recording worker."""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from config.config_store import CameraConfig
from recorder.ffmpeg_runner import FFmpegRunner
from utils.disk_cleanup import cleanup_for_free_space

CAMERA_STATES = ["未設定", "無効", "停止中", "接続確認中", "録画中", "区切り中", "再接続中", "エラー"]


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class CameraRuntimeStatus:
    id: int
    name: str
    enabled: bool
    recording_status: str = "停止中"
    last_completed_file: str = ""
    current_segment_start: str = ""
    last_error: str = ""
    last_update: str = field(default_factory=lambda: fmt_dt(datetime.now()))

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


class CameraRecorder:
    def __init__(self, camera: CameraConfig, settings: dict, runner: FFmpegRunner, logger: logging.Logger) -> None:
        self.camera = camera
        self.settings = settings
        self.runner = runner
        self.logger = logger.getChild(f"cam{camera.id:02d}")
        initial = "無効" if not camera.enabled else ("未設定" if not camera.rtsp_url else "停止中")
        self.status = CameraRuntimeStatus(camera.id, camera.name, camera.enabled, recording_status=initial)
        self._stop_event = threading.Event()
        self._split_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._process = None
        self._current_temp: Path | None = None
        self._segment_start: datetime | None = None

    def start(self) -> None:
        if not self.camera.enabled or not self.camera.rtsp_url:
            self._set_status("無効" if not self.camera.enabled else "未設定")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._split_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name=f"CameraRecorder-{self.camera.id}", daemon=True)
        self._thread.start()
        self.logger.info("録画開始要求")

    def stop(self) -> None:
        self._stop_event.set()
        self._split_event.set()
        self._stop_process()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=15)
        self._set_status("停止中")
        self.logger.info("録画停止")

    def split_now(self, reason: str = "手動区切り") -> None:
        if self.is_recording:
            self.logger.info("MP4区切り要求: %s", reason)
            self._split_event.set()

    @property
    def is_recording(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self.status.as_dict()

    def test_connection(self) -> tuple[bool, str]:
        self._set_status("接続確認中")
        ok, message = self.runner.test_rtsp(self.camera.rtsp_url)
        self._set_error("" if ok else message)
        self._set_status("停止中" if ok else "エラー")
        return ok, message

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._segment_start = datetime.now().replace(microsecond=0)
            self._current_temp = self._temp_path(self._segment_start)
            self._current_temp.parent.mkdir(parents=True, exist_ok=True)
            self._set_status("録画中", current_segment_start=fmt_dt(self._segment_start), error="")
            try:
                self._process = self.runner.start_recording(self.camera.rtsp_url, self._current_temp)
            except FileNotFoundError as exc:
                self._set_status("エラー", error=f"FFmpegが見つかりません: {exc}")
                self.logger.error("録画開始失敗: %s", exc)
                time.sleep(10)
                continue

            deadline = self._segment_start + timedelta(minutes=self.camera.segment_minutes)
            manual_split = False
            while not self._stop_event.is_set():
                if self._process.poll() is not None:
                    stderr = self._process.stderr.read() if self._process.stderr else ""
                    self._set_status("再接続中", error=stderr[-500:])
                    self.logger.warning("FFmpeg終了、再接続します: %s", stderr[-500:])
                    break
                if self._split_event.is_set():
                    manual_split = True
                    self._split_event.clear()
                    self.logger.info("手動区切り実行: %s", fmt_dt(datetime.now()))
                    break
                if datetime.now() >= deadline:
                    break
                time.sleep(0.5)

            end_time = datetime.now().replace(microsecond=0)
            self._set_status("区切り中")
            self._stop_process()
            if self._current_temp and self._current_temp.exists() and self._current_temp.stat().st_size > 0 and self._segment_start:
                try:
                    cleanup_for_free_space(self.settings["archive_dir"], float(self.settings.get("min_free_gb", 5120)), self.logger)
                    dest = self._archive_path(self._segment_start, end_time)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(self._current_temp, dest)
                    self._set_last_file(str(dest))
                    self.logger.info("MP4完成%s: %s", "（手動区切り）" if manual_split else "", dest)
                except OSError:
                    dest = self._archive_path(self._segment_start, end_time)
                    shutil.move(str(self._current_temp), str(dest))
                    self._set_last_file(str(dest))
                    self.logger.info("Archive移動完了(copy fallback): %s", dest)
            elif not self._stop_event.is_set():
                self.logger.warning("完成ファイルなし: %s", self._current_temp)
            if self._stop_event.is_set():
                break
        self._set_status("停止中", current_segment_start="")

    def _stop_process(self) -> None:
        proc = self._process
        if proc is None:
            return
        code, stderr = self.runner.stop_recording(proc)
        if stderr and code not in (0, None):
            self._set_error(stderr[-500:])
        self._process = None

    def _temp_path(self, start: datetime) -> Path:
        return Path(self.settings["temp_dir"]) / self.camera.save_subdir / f"recording_{start:%Y%m%d_%H%M%S}.partial"

    def _archive_path(self, start: datetime, end: datetime) -> Path:
        filename = f"{self.camera.save_subdir}_{start:%Y%m%d_%H%M%S}_{end:%H%M%S}.mp4"
        return Path(self.settings["archive_dir"]) / self.camera.save_subdir / f"{start:%Y-%m-%d}" / filename

    def _set_status(self, state: str, current_segment_start: str | None = None, error: str | None = None) -> None:
        with self._lock:
            self.status.recording_status = state
            self.status.last_update = fmt_dt(datetime.now())
            if current_segment_start is not None:
                self.status.current_segment_start = current_segment_start
            if error is not None:
                self.status.last_error = error

    def _set_error(self, error: str) -> None:
        with self._lock:
            self.status.last_error = error
            self.status.last_update = fmt_dt(datetime.now())

    def _set_last_file(self, path: str) -> None:
        with self._lock:
            self.status.last_completed_file = path
            self.status.last_update = fmt_dt(datetime.now())
