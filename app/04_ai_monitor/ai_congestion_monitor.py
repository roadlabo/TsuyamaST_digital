from __future__ import annotations

# =========================================
# Imports
# =========================================
import argparse
import csv
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay")
os.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

import cv2
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.page import PageMargins
from PyQt6 import QtCore, QtGui, QtWidgets
from ultralytics import YOLO

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from importlib import import_module
_congestion_common = import_module("10_common.congestion_common")
CongestionSmoother = _congestion_common.CongestionSmoother
level_style = _congestion_common.level_style


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ai_monitor.log"
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    root.addHandler(file_handler)


def _sleep_backoff(i: int, cap: float = 0.5) -> None:
    time.sleep(min(cap, 0.05 * (2 ** i)))


def safe_replace(tmp_path: Path, dst_path: Path, retries: int = 10) -> None:
    last_exc: Exception | None = None
    for i in range(max(1, int(retries))):
        try:
            os.replace(tmp_path, dst_path)
            return
        except (PermissionError, OSError) as exc:
            last_exc = exc
            logging.warning("[WARN] ai_status write retry %d/%d failed: %s", i + 1, retries, exc)
            _sleep_backoff(i)
    try:
        tmp_path.replace(dst_path)
        return
    except Exception as exc:
        raise RuntimeError(f"safe_replace failed: {tmp_path} -> {dst_path} ({last_exc or exc})") from exc


def write_json_atomic(path: Path, payload: dict[str, Any], retries: int = 10) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        safe_replace(tmp_path, path, retries=retries)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def safe_read_json(path: Path, default: Any, retries: int = 3) -> Any:
    if not path.exists():
        return default
    last_exc: Exception | None = None
    for i in range(max(1, int(retries))):
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, PermissionError, OSError) as exc:
            last_exc = exc
            _sleep_backoff(i, cap=0.2)
        except Exception as exc:
            last_exc = exc
            break
    logging.warning("safe_read_json failed: %s (%s)", path, last_exc)
    return default

# =========================================
# Constants / CLASS_MAP
# =========================================
CLASS_MAP = {
    0: "人",
    1: "自転車",
    2: "車",
    3: "オートバイ",
    4: "飛行機",
    5: "バス",
    6: "電車",
    7: "トラック",
}

# =========================================
# Default Settings
# =========================================
DEFAULT_SYSTEM_CONFIG: dict[str, Any] = {
    "model_path": "yolo11m.pt",
    "device_preference": "auto",
    "metrics_save_interval_sec": 5,
    "ui_refresh_interval_ms": 500,
    "ai_status_json_path": "app/11_config/ai_status.json",
    "status_update_interval_sec": 10,
    "output_root": "app/04_ai_monitor/data",
    "display_update_interval_ms": 800,
    "graph_update_interval_sec": 10,
    "level2_threshold": 8.0,
    "level3_threshold": 12.0,
    "level4_threshold": 16.0,
    "congestion_smoothing_window": 6,
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
            "congestion_threshold": 5,
            "long_stay_minutes": 15,
            "long_stay_trigger_count": 1,
            "stay_zone_polygon": [],
            "reconnect_sec": 3,
            "tracking_method": "bytetrack",
            "yolo_model": "yolo11m.pt",
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
            "congestion_calculation_interval": 3,
            "enable_congestion": True,
            "line_direction_mode": "line_vector",
            "display_scale": 0.8,
        },
        {
            "camera_id": 2,
            "camera_name": "Camera2",
            "stream_url": "1",
            "enabled": True,
            "line_start": [100, 300],
            "line_end": [1000, 300],
            "exclude_polygon": [],
            "congestion_threshold": 5,
            "long_stay_minutes": 15,
            "long_stay_trigger_count": 1,
            "stay_zone_polygon": [],
            "reconnect_sec": 3,
            "tracking_method": "bytetrack",
            "yolo_model": "yolo11m.pt",
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
            "congestion_calculation_interval": 3,
            "enable_congestion": True,
            "line_direction_mode": "line_vector",
            "display_scale": 0.8,
        },
        {
            "camera_id": 3,
            "camera_name": "Camera3",
            "stream_url": "2",
            "enabled": True,
            "line_start": [100, 300],
            "line_end": [1000, 300],
            "exclude_polygon": [],
            "congestion_threshold": 5,
            "long_stay_minutes": 15,
            "long_stay_trigger_count": 1,
            "stay_zone_polygon": [],
            "reconnect_sec": 3,
            "tracking_method": "bytetrack",
            "yolo_model": "yolo11m.pt",
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
            "congestion_calculation_interval": 3,
            "enable_congestion": True,
            "line_direction_mode": "line_vector",
            "display_scale": 0.8,
        },
    ]
}

KEEP_CAMERA_KEYS = [
    "camera_id",
    "camera_name",
    "stream_url",
    "enabled",
    "line_start",
    "line_end",
    "exclude_polygon",
    "stay_zone_polygon",
    "congestion_threshold",
    "long_stay_minutes",
]

# =========================================
# Dataclasses
# =========================================
@dataclass
class AppConfig:
    root_dir: Path
    system_config_path: Path
    camera_settings_path: Path
    system: dict[str, Any]
    cameras: list[dict[str, Any]]


@dataclass
class CongestionState:
    frame_motion_scores: list[float] = field(default_factory=list)
    smoothed_motion_scores: list[float] = field(default_factory=list)
    frame_time_stamps: list[datetime] = field(default_factory=list)
    frame_cumulative_motion_score: float = 0.0
    window_frame_count: int = 0
    current_congestion_index: float = 0.0
    current_smoothed_index: float = 0.0
    window_start: datetime | None = None
    previous_positions: dict[int, tuple[float, float]] = field(default_factory=dict)
    last_seen_at: dict[int, datetime] = field(default_factory=dict)


@dataclass
class CounterState:
    counted_track_ids: set[int] = field(default_factory=set)
    previous_side: dict[int, float] = field(default_factory=dict)
    pass_bins_ltor: list[int] = field(default_factory=lambda: [0] * 144)
    pass_bins_rtol: list[int] = field(default_factory=lambda: [0] * 144)


@dataclass
class TrackState:
    first_seen: dict[int, datetime] = field(default_factory=dict)
    long_stay_emitted: set[int] = field(default_factory=set)


@dataclass
class WakimuraAlphaState:
    exit_timestamps: list[datetime] = field(default_factory=list)
    high_load_active: bool = False
    high_load_session_stays: list[float] = field(default_factory=list)
    high_load_alpha: float | None = None
    current_alpha: float = 0.0
    current_alpha_window: float = 0.0
    current_n_out: int = 0
    current_avg_stay_sec: float = 0.0


# =========================================
# Config Manager
# =========================================
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
            slim_defaults = [self._to_slim_camera_settings(cam) for cam in DEFAULT_CAMERA_SETTINGS.get("cameras", [])]
            self._atomic_write_json(self.camera_settings_path, {"cameras": slim_defaults})

    def load(self) -> AppConfig:
        self.ensure_defaults()
        system = safe_read_json(self.system_config_path, DEFAULT_SYSTEM_CONFIG.copy(), retries=3)
        camera_dict = safe_read_json(self.camera_settings_path, {"cameras": []}, retries=3)
        loaded_cameras = camera_dict.get("cameras", [])
        default_map = {int(c["camera_id"]): dict(c) for c in DEFAULT_CAMERA_SETTINGS.get("cameras", [])}
        cameras: list[dict[str, Any]] = []
        for cam in loaded_cameras:
            raw = dict(cam)
            if "line_points" in raw and "line_start" not in raw and "line_end" not in raw:
                points = raw.get("line_points") or []
                if isinstance(points, list) and len(points) >= 2:
                    raw["line_start"] = points[0]
                    raw["line_end"] = points[1]
            cid = int(raw.get("camera_id", -1))
            base = dict(default_map.get(cid, {}))
            base.update(raw)
            base.pop("line_points", None)
            base.pop("direction", None)
            cameras.append(base)
        return AppConfig(self.root_dir, self.system_config_path, self.camera_settings_path, system, cameras)

    def save_camera_settings(self, cameras: list[dict[str, Any]]) -> None:
        slim = [self._to_slim_camera_settings(cam) for cam in cameras]
        self._atomic_write_json(self.camera_settings_path, {"cameras": slim})

    def save_system_settings(self, system: dict[str, Any]) -> None:
        self._atomic_write_json(self.system_config_path, system)

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        write_json_atomic(path, data, retries=10)

    @staticmethod
    def _to_slim_camera_settings(camera: dict[str, Any]) -> dict[str, Any]:
        return {k: camera[k] for k in KEEP_CAMERA_KEYS if k in camera}


# =========================================
# Congestion Logic
# =========================================
class CongestionScorer:
    """AICount11.py の congestion 算出式を監視向けに時間窓化して適用。3秒窓のフレーム平均で更新する。"""

    def __init__(self, interval_sec: int = 3, day_keep: int = 1, smoothing_window: int = 6):
        self.interval_sec = max(1, int(interval_sec))
        self.day_keep = max(1, day_keep)
        self.state = CongestionState()
        self.smoother = CongestionSmoother(smoothing_window)

    def update_interval(self, interval_sec: int) -> None:
        self.interval_sec = max(1, int(interval_sec))

    def update_smoothing_window(self, window_size: int) -> None:
        self.smoother.update_window_size(window_size)

    def _compute_frame_motion_score(self, tracks: list[dict], frame_width: int, now: datetime) -> float:
        if frame_width <= 0:
            return 0.0

        total = 0.0
        for tr in tracks:
            track_id = int(tr["track_id"])
            cx, cy = tr["center"]
            prev_x, prev_y = self.state.previous_positions.get(track_id, (cx, cy))
            distance = ((cx - prev_x) ** 2 + (cy - prev_y) ** 2) ** 0.5
            total += 1.0 / (1.0 + (distance / frame_width) * 500.0)

            self.state.previous_positions[track_id] = (cx, cy)
            self.state.last_seen_at[track_id] = now

        stale_before = now - timedelta(seconds=max(10, self.interval_sec * 3))
        stale_ids = [tid for tid, ts in self.state.last_seen_at.items() if ts < stale_before]
        for tid in stale_ids:
            self.state.last_seen_at.pop(tid, None)
            self.state.previous_positions.pop(tid, None)

        return total

    def update(self, tracks: list[dict], now: datetime, frame_width: int) -> float:
        if self.state.window_start is None:
            self.state.window_start = now
        frame_score = self._compute_frame_motion_score(tracks, frame_width, now)
        self.state.frame_cumulative_motion_score += frame_score
        self.state.window_frame_count += 1
        elapsed = (now - self.state.window_start).total_seconds()
        if elapsed < self.interval_sec:
            return self.state.current_congestion_index

        frame_count = max(1, self.state.window_frame_count)
        value = round(self.state.frame_cumulative_motion_score / frame_count, 3)
        self.state.frame_motion_scores.append(value)
        self.state.frame_time_stamps.append(now)
        self.state.current_congestion_index = value
        self.state.current_smoothed_index = round(self.smoother.add(value), 3)
        self.state.smoothed_motion_scores.append(self.state.current_smoothed_index)
        self.state.frame_cumulative_motion_score = 0.0
        self.state.window_frame_count = 0
        self.state.window_start = now

        day_ago = now - timedelta(days=self.day_keep)
        while self.state.frame_time_stamps and self.state.frame_time_stamps[0] < day_ago:
            self.state.frame_time_stamps.pop(0)
            self.state.frame_motion_scores.pop(0)
            if self.state.smoothed_motion_scores:
                self.state.smoothed_motion_scores.pop(0)
        return value


# =========================================
# Line Counter Logic
# =========================================
class LineCounter:
    def __init__(self, line_points: list[list[int]]):
        self.line_points = line_points
        self.state = CounterState()

    def update_line(self, line_points: list[list[int]]) -> None:
        self.line_points = line_points
        self.state.previous_side.clear()
        self.state.counted_track_ids.clear()

    def _signed_side(self, point: tuple[float, float]) -> float:
        p1, p2 = self.line_points
        vx, vy = p2[0] - p1[0], p2[1] - p1[1]
        wx, wy = point[0] - p1[0], point[1] - p1[1]
        return vx * wy - vy * wx

    def update(self, track_id: int, point: tuple[float, float], class_name: str, now: datetime):
        if len(self.line_points) != 2:
            return None
        current_side = self._signed_side(point)
        prev_side = self.state.previous_side.get(track_id)
        self.state.previous_side[track_id] = current_side
        if prev_side is None or track_id in self.state.counted_track_ids:
            return None

        crossed = (prev_side < 0 <= current_side) or (prev_side > 0 >= current_side)
        if not crossed:
            return None

        direction = "LtoR" if prev_side < current_side else "RtoL"
        self.state.counted_track_ids.add(track_id)
        bin_index = (now.hour * 60 + now.minute) // 10
        if 0 <= bin_index < 144:
            if direction == "LtoR":
                self.state.pass_bins_ltor[bin_index] += 1
            else:
                self.state.pass_bins_rtol[bin_index] += 1

        return {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "track_id": track_id,
            "class_name": class_name,
            "direction": direction,
        }


class WakimuraAlphaCalculator:
    """20_rotary_efficiency_analysis.py を基にした参考指標（LEVEL判定には不使用）。"""

    def __init__(
        self,
        rotary_capacity: int = 10,
        base_stay_time_sec: float = 60.0,
        window_seconds: int = 300,
        high_load_vehicle_threshold: int = 7,
    ):
        self.rotary_capacity = max(1, int(rotary_capacity))
        self.base_stay_time_sec = max(1e-6, float(base_stay_time_sec))
        self.window_seconds = max(1, int(window_seconds))
        self.high_load_vehicle_threshold = max(1, int(high_load_vehicle_threshold))
        self.state = WakimuraAlphaState()

    def record_exit(self, now: datetime) -> None:
        self.state.exit_timestamps.append(now)

    def update(self, now: datetime, vehicle_count: int, avg_stay_sec: float) -> dict[str, Any]:
        window_start = now - timedelta(seconds=self.window_seconds)
        self.state.exit_timestamps = [ts for ts in self.state.exit_timestamps if ts >= window_start]
        n_out = len(self.state.exit_timestamps)
        c_nominal = 3600.0 * self.rotary_capacity / self.base_stay_time_sec
        c_effective = 3600.0 * n_out / float(self.window_seconds)
        alpha_window = (c_effective / c_nominal) if c_nominal > 0 else 0.0

        if vehicle_count >= self.high_load_vehicle_threshold:
            if not self.state.high_load_active:
                self.state.high_load_active = True
                self.state.high_load_session_stays.clear()
                self.state.high_load_alpha = None
            if avg_stay_sec > 0.0:
                self.state.high_load_session_stays.append(float(avg_stay_sec))
            if self.state.high_load_session_stays:
                session_avg = float(np.mean(self.state.high_load_session_stays))
                if session_avg > 0.0:
                    self.state.high_load_alpha = self.base_stay_time_sec / session_avg
        else:
            self.state.high_load_active = False
            self.state.high_load_session_stays.clear()
            self.state.high_load_alpha = None

        alpha = alpha_window
        if self.state.high_load_active and self.state.high_load_alpha is not None:
            alpha = self.state.high_load_alpha

        self.state.current_alpha = float(alpha)
        self.state.current_alpha_window = float(alpha_window)
        self.state.current_n_out = int(n_out)
        self.state.current_avg_stay_sec = max(0.0, float(avg_stay_sec))
        return {
            "wakimura_alpha": float(self.state.current_alpha),
            "wakimura_alpha_window": float(self.state.current_alpha_window),
            "wakimura_n_out": int(self.state.current_n_out),
            "wakimura_avg_stay_sec": float(self.state.current_avg_stay_sec),
            "wakimura_high_load_mode": bool(self.state.high_load_active),
        }


# =========================================
# Status Manager
# =========================================
class StatusManager:
    def __init__(self, ai_status_path: Path, system_cfg: dict[str, Any]):
        self.ai_status_path = ai_status_path
        self.system_cfg = system_cfg
        self._last_level: int | None = None
        self._last_write_time: float = 0.0
        self.last_failure_at: float = 0.0

    def update_if_needed(self, level: int) -> None:
        now = time.time()
        update_interval_sec = max(1, int(self.system_cfg.get("status_update_interval_sec", 10)))
        if level == self._last_level and (now - self._last_write_time) < update_interval_sec:
            return
        payload = {
            "congestion_level": int(level),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            self._atomic_write_json(self.ai_status_path, payload)
        except Exception as exc:
            now_ts = time.time()
            if now_ts - self.last_failure_at > 5.0:
                self.last_failure_at = now_ts
                logging.warning("ai_status write skipped: %s", exc)
            return
        self._last_level = int(level)
        self._last_write_time = now

    @staticmethod
    def _atomic_write_json(path: Path, data: dict) -> None:
        write_json_atomic(path, data, retries=10)


# =========================================
# Report Writer
# =========================================
class ReportWriter:
    def __init__(self, output_root: Path):
        self.output_root = output_root

    def write_daily_report(self, target_date: date, cameras: list[dict], metrics_root: Path) -> Path:
        wb = Workbook()
        ws_summary = wb.active
        ws_summary.title = "summary"
        self._style_sheet(ws_summary)

        ws_summary["A1"] = f"Daily Report {target_date.isoformat()}"
        ws_summary["A1"].font = Font(bold=True, size=16, color="00D7FF")
        headers = ["camera", "total_pass", "max_congestion", "over_threshold_points", "long_stay_events"]
        ws_summary.append(headers)

        total_pass_all = 0
        for cam in cameras:
            cid = cam["camera_id"]
            date_str = target_date.isoformat()
            cam_dir = metrics_root / f"cam{cid}"
            pass_df = self._safe_read(cam_dir / f"pass_events_{date_str}.csv")
            metric_df = self._safe_read(cam_dir / f"congestion_timeseries_{date_str}.csv")
            long_df = self._safe_read(cam_dir / f"long_stay_events_{date_str}.csv")

            total_pass = len(pass_df)
            total_pass_all += total_pass
            max_cong = float(metric_df["congestion_score"].max()) if not metric_df.empty else 0.0
            over_count = int((metric_df["congestion_score"] > metric_df["congestion_threshold"]).sum()) if not metric_df.empty else 0
            long_count = len(long_df)
            ws_summary.append([cam["camera_name"], total_pass, round(max_cong, 2), over_count, long_count])
            self._add_camera_sheet(wb, cam, pass_df, metric_df, long_df)

        ws_summary["A2"] = "date"
        ws_summary["B2"] = target_date.isoformat()
        ws_summary["A3"] = "all_cameras_total_pass"
        ws_summary["B3"] = total_pass_all

        out_path = self.output_root / "reports" / "daily" / f"daily_report_{target_date.isoformat()}.xlsx"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(out_path)
        return out_path

    def write_monthly_report(self, target_month: str, metrics_root: Path, cameras: list[dict]) -> Path:
        wb = Workbook()
        ws = wb.active
        ws.title = "monthly"
        self._style_sheet(ws)
        ws.append(["date", "total_pass", "max_congestion", "long_stay_events"])

        daily_map: dict[str, dict[str, float]] = {}
        for cam in cameras:
            cam_dir = metrics_root / f"cam{cam['camera_id']}"
            for file in cam_dir.glob(f"congestion_timeseries_{target_month}-*.csv"):
                date_str = file.stem.split("_")[-1]
                d = daily_map.setdefault(date_str, {"pass": 0, "max": 0, "long": 0})
                metric_df = self._safe_read(file)
                pass_df = self._safe_read(cam_dir / f"pass_events_{date_str}.csv")
                long_df = self._safe_read(cam_dir / f"long_stay_events_{date_str}.csv")
                d["pass"] += len(pass_df)
                if not metric_df.empty:
                    d["max"] = max(d["max"], float(metric_df["congestion_score"].max()))
                d["long"] += len(long_df)

        for key in sorted(daily_map.keys()):
            d = daily_map[key]
            ws.append([key, int(d["pass"]), round(d["max"], 2), int(d["long"])])

        out_path = self.output_root / "reports" / "monthly" / f"monthly_report_{target_month}.xlsx"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(out_path)
        return out_path

    def _add_camera_sheet(self, wb: Workbook, cam: dict, pass_df: pd.DataFrame, metric_df: pd.DataFrame, long_df: pd.DataFrame) -> None:
        ws = wb.create_sheet(f"cam{cam['camera_id']}")
        self._style_sheet(ws)
        ws["A1"] = f"{cam['camera_name']} Detail"
        ws["A1"].font = Font(bold=True, size=14, color="00D7FF")

        hist = [0] * 144
        if not pass_df.empty:
            pass_df["timestamp"] = pd.to_datetime(pass_df["timestamp"])
            for ts in pass_df["timestamp"]:
                hist[(ts.hour * 60 + ts.minute) // 10] += 1

        ws.append(["bin", "pass_count"])
        for i, v in enumerate(hist):
            ws.append([i, v])

        bar = BarChart()
        bar.title = "Pass Histogram"
        data = Reference(ws, min_col=2, min_row=3, max_row=146)
        cats = Reference(ws, min_col=1, min_row=3, max_row=146)
        bar.add_data(data, titles_from_data=False)
        bar.set_categories(cats)
        bar.height = 6
        bar.width = 13
        ws.add_chart(bar, "D3")

        start_row = 150
        ws[f"A{start_row}"] = "congestion_time_series"
        ws.append(["timestamp", "congestion_score"])
        for _, r in metric_df[["timestamp", "congestion_score"]].iterrows() if not metric_df.empty else []:
            ws.append([r["timestamp"], float(r["congestion_score"])])

        if len(metric_df) > 2:
            line = LineChart()
            line.title = "Congestion Score"
            dref = Reference(ws, min_col=2, min_row=start_row + 1, max_row=start_row + len(metric_df))
            cref = Reference(ws, min_col=1, min_row=start_row + 1, max_row=start_row + len(metric_df))
            line.add_data(dref, titles_from_data=False)
            line.set_categories(cref)
            line.height = 5
            line.width = 13
            ws.add_chart(line, "D18")

        ls_row = 18
        ws[f"A{ls_row}"] = "long_stay_list"
        ws[f"A{ls_row+1}"] = "track_id"
        ws[f"B{ls_row+1}"] = "stay_minutes"
        for i, (_, r) in enumerate(long_df.iterrows() if not long_df.empty else []):
            ws[f"A{ls_row+2+i}"] = int(r["track_id"])
            ws[f"B{ls_row+2+i}"] = float(r["stay_minutes"])

    def _style_sheet(self, ws):
        fill = PatternFill("solid", fgColor="101922")
        thin = Side(style="thin", color="1A9FB6")
        for col in ["A", "B", "C", "D", "E", "F", "G"]:
            ws.column_dimensions[col].width = 22
        for row in range(1, 220):
            for col in range(1, 8):
                c = ws.cell(row=row, column=col)
                c.fill = fill
                c.font = Font(color="FFFFFF", size=10)
                c.alignment = Alignment(vertical="center")
                c.border = Border(left=thin, right=thin, top=thin, bottom=thin)
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.orientation = ws.ORIENTATION_PORTRAIT
        ws.page_margins = PageMargins(left=0.4, right=0.4, top=0.5, bottom=0.5)

    @staticmethod
    def _safe_read(path: Path) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame()
        try:
            return pd.read_csv(path)
        except Exception:
            return pd.DataFrame()


class TenMinuteRecordWriter:
    HEADERS = [
        "時刻（10分単位）",
        "渋滞レベル",
        "Camera1 渋滞指数",
        "Camera1 脇村指標α",
        "Camera1 LtoR",
        "Camera1 RtoL",
        "Camera2 渋滞指数",
        "Camera2 脇村指標α",
        "Camera2 LtoR",
        "Camera2 RtoL",
        "Camera3 渋滞指数",
        "Camera3 脇村指標α",
        "Camera3 LtoR",
        "Camera3 RtoL",
    ]

    def __init__(self, output_root: Path):
        self.output_root = output_root / "reports" / "daily"

    @staticmethod
    def _floor_to_10min(dt: datetime) -> datetime:
        return dt.replace(minute=(dt.minute // 10) * 10, second=0, microsecond=0)

    def collect_sample(self, now: datetime, level: int, latest_payloads: dict[int, dict[str, Any]]) -> dict[str, Any]:
        sample: dict[str, Any] = {"time": now, "level": int(level), "cameras": {}}
        for cid in (1, 2, 3):
            payload = latest_payloads.get(cid, {})
            sample["cameras"][cid] = {
                "index": float(payload.get("smoothed_congestion_score", payload.get("congestion_score", 0.0))),
                "wakimura_alpha": float(payload.get("wakimura_alpha", 0.0)),
                "ltor_total": int(payload.get("count_ltor", 0)),
                "rtol_total": int(payload.get("count_rtol", 0)),
            }
        return sample

    def aggregate_10min(self, bin_start: datetime, samples: list[dict[str, Any]]) -> list[Any]:
        if not samples:
            return [bin_start.strftime("%H:%M"), 1, 0.0, 0.0, 0, 0, 0.0, 0.0, 0, 0, 0.0, 0.0, 0, 0]
        level_max = max(int(s["level"]) for s in samples)
        row: list[Any] = [bin_start.strftime("%H:%M"), level_max]
        for cid in (1, 2, 3):
            indices = [float(s["cameras"][cid]["index"]) for s in samples]
            wakimuras = [float(s["cameras"][cid]["wakimura_alpha"]) for s in samples]
            ltor_values = [int(s["cameras"][cid]["ltor_total"]) for s in samples]
            rtol_values = [int(s["cameras"][cid]["rtol_total"]) for s in samples]
            ltor = max(0, max(ltor_values) - min(ltor_values)) if ltor_values else 0
            rtol = max(0, max(rtol_values) - min(rtol_values)) if rtol_values else 0
            avg_index = round(float(np.mean(indices)), 3) if indices else 0.0
            avg_wakimura = round(float(np.mean(wakimuras)), 3) if wakimuras else 0.0
            row.extend([avg_index, avg_wakimura, ltor, rtol])
        return row

    def append_row(self, target_date: date, row: list[Any]) -> Path:
        wb, path = self._load_or_create(target_date)
        ws = wb["Data"]
        ws.append(row)
        wb.save(path)
        return path

    def finalize_day_graph(self, target_date: date) -> Path | None:
        path = self.output_root / f"{target_date.isoformat()}.xlsx"
        if not path.exists():
            return None
        wb = load_workbook(path)
        ws_data = wb["Data"] if "Data" in wb.sheetnames else wb.active
        if "Graph" in wb.sheetnames:
            del wb["Graph"]
        ws_graph = wb.create_sheet("Graph")
        last_row = ws_data.max_row
        if last_row < 2:
            wb.save(path)
            return path

        cats = Reference(ws_data, min_col=1, min_row=2, max_row=last_row)

        chart_level = LineChart()
        chart_level.title = "渋滞レベル推移"
        chart_level.y_axis.scaling.min = 1
        chart_level.y_axis.scaling.max = 4
        chart_level.height = 6
        chart_level.width = 10
        chart_level.add_data(Reference(ws_data, min_col=2, min_row=1, max_row=last_row), titles_from_data=True)
        chart_level.set_categories(cats)
        ws_graph.add_chart(chart_level, "A1")

        chart_index = LineChart()
        chart_index.title = "カメラ別渋滞指数"
        chart_index.height = 6
        chart_index.width = 10
        for col in (3, 7, 11):
            chart_index.add_data(Reference(ws_data, min_col=col, max_col=col, min_row=1, max_row=last_row), titles_from_data=True)
        chart_index.set_categories(cats)
        ws_graph.add_chart(chart_index, "A18")

        chart_traffic = BarChart()
        chart_traffic.title = "交通量（LtoR / RtoL）"
        chart_traffic.height = 6
        chart_traffic.width = 10
        for col in (5, 6, 9, 10, 13, 14):
            chart_traffic.add_data(Reference(ws_data, min_col=col, max_col=col, min_row=1, max_row=last_row), titles_from_data=True)
        chart_traffic.set_categories(cats)
        ws_graph.add_chart(chart_traffic, "L1")

        ws_graph.page_setup.paperSize = ws_graph.PAPERSIZE_A4
        ws_graph.page_setup.orientation = ws_graph.ORIENTATION_LANDSCAPE
        ws_graph.page_setup.fitToWidth = 1
        ws_graph.page_setup.fitToHeight = 1
        ws_graph.page_margins = PageMargins(left=0.2, right=0.2, top=0.3, bottom=0.3)
        wb.save(path)
        return path

    def _load_or_create(self, target_date: date) -> tuple[Workbook, Path]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        path = self.output_root / f"{target_date.isoformat()}.xlsx"
        if path.exists():
            wb = load_workbook(path)
            ws = wb["Data"] if "Data" in wb.sheetnames else wb.active
            existing_headers = [ws.cell(row=1, column=i + 1).value for i in range(len(self.HEADERS))]
            if existing_headers != self.HEADERS:
                wb = Workbook()
                ws = wb.active
                ws.title = "Data"
                ws.append(self.HEADERS)
                wb.create_sheet("Graph")
                wb.save(path)
            return wb, path
        wb = Workbook()
        ws = wb.active
        ws.title = "Data"
        ws.append(self.HEADERS)
        wb.create_sheet("Graph")
        wb.save(path)
        return wb, path


# =========================================
# Graph Utilities
# =========================================
def _normalize_time(ts: pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(year=1900, month=1, day=1, hour=ts.hour, minute=ts.minute, second=ts.second)


def build_multi_day_trend(metrics_files: list[Path], metric_col: str) -> pd.DataFrame:
    frames = []
    for path in metrics_files:
        if not path.exists():
            continue
        df = pd.read_csv(path)
        if "timestamp" not in df.columns or metric_col not in df.columns:
            continue
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df["time_normalized"] = df["timestamp"].apply(_normalize_time)
        frames.append(df[["time_normalized", metric_col]])

    if not frames:
        return pd.DataFrame(columns=["time_normalized", "avg", "median"])

    merged = pd.concat(frames, ignore_index=True)
    merged["time_rounded"] = merged["time_normalized"].dt.floor("1min")
    grouped = merged.groupby("time_rounded")[metric_col]
    out = grouped.mean().rename("avg").to_frame()
    out["median"] = grouped.median()
    return out.reset_index().rename(columns={"time_rounded": "time_normalized"})


def save_multi_day_trend_plot(metrics_files: list[Path], metric_col: str, output_png: Path) -> None:
    trend = build_multi_day_trend(metrics_files, metric_col)
    if trend.empty:
        return
    output_png.parent.mkdir(parents=True, exist_ok=True)

    plt.figure(figsize=(12, 4.5))
    plt.plot(trend["time_normalized"], trend["avg"], label="Average", linewidth=2)
    plt.plot(trend["time_normalized"], trend["median"], label="Median", linewidth=2)
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=1))
    plt.xticks(rotation=45)
    plt.grid(alpha=0.3)
    plt.title(f"Multi-day trend: {metric_col}")
    plt.tight_layout()
    plt.legend()
    plt.savefig(output_png, dpi=150)
    plt.close()


# =========================================
# UI Panels
# =========================================
class ClickableImageLabel(QtWidgets.QLabel):
    point_clicked = QtCore.pyqtSignal(int, int)

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.source_size = (1, 1)

    def set_source_size(self, width: int, height: int) -> None:
        self.source_size = (max(1, width), max(1, height))

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        pix = self.pixmap()
        if pix is None:
            return
        scaled = pix.size()
        off_x = (self.width() - scaled.width()) // 2
        off_y = (self.height() - scaled.height()) // 2
        local_x = event.pos().x() - off_x
        local_y = event.pos().y() - off_y
        if local_x < 0 or local_y < 0 or local_x >= scaled.width() or local_y >= scaled.height():
            return
        src_w, src_h = self.source_size
        x = int(local_x * src_w / max(1, scaled.width()))
        y = int(local_y * src_h / max(1, scaled.height()))
        self.point_clicked.emit(x, y)
        super().mousePressEvent(event)


def _segments_intersect(p1, p2, p3, p4) -> bool:
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])
    return ccw(p1, p3, p4) != ccw(p2, p3, p4) and ccw(p1, p2, p3) != ccw(p1, p2, p4)


def has_self_intersection(points: list[list[int]]) -> bool:
    if len(points) < 4:
        return False
    n = len(points)
    for i in range(n):
        a1 = points[i]
        a2 = points[(i + 1) % n]
        for j in range(i + 1, n):
            if abs(i - j) <= 1 or (i == 0 and j == n - 1):
                continue
            b1 = points[j]
            b2 = points[(j + 1) % n]
            if _segments_intersect(a1, a2, b1, b2):
                return True
    return False


class CameraSettingsDialog(QtWidgets.QDialog):
    def __init__(self, camera_cfg: dict[str, Any], latest_frame=None, parent=None, initial_mode: str = "line"):
        super().__init__(parent)
        self.setWindowTitle(f"設定: {camera_cfg['camera_name']}")
        self.resize(1000, 780)
        self.camera_cfg = dict(camera_cfg)
        self.line_points = [camera_cfg.get("line_start", [100, 100])[:], camera_cfg.get("line_end", [400, 100])[:]]
        self.exclude_polygon: list[list[int]] = [p[:] for p in camera_cfg.get("exclude_polygon", [])]
        self.mode = initial_mode if initial_mode in {"line", "poly"} else "line"

        root = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget()
        root.addWidget(tabs)

        basic = QtWidgets.QWidget()
        tabs.addTab(basic, "基本")
        form = QtWidgets.QFormLayout(basic)
        self.name_edit = QtWidgets.QLineEdit(camera_cfg.get("camera_name", ""))
        self.url_edit = QtWidgets.QLineEdit(str(camera_cfg.get("stream_url", "")))
        form.addRow("カメラ名", self.name_edit)
        form.addRow("stream_url", self.url_edit)

        ai_tab = QtWidgets.QWidget()
        tabs.addTab(ai_tab, "解析条件")
        ai_form = QtWidgets.QFormLayout(ai_tab)

        self.yolo_model = QtWidgets.QLineEdit(str(camera_cfg.get("yolo_model", "yolo11m.pt")))
        self.conf = QtWidgets.QDoubleSpinBox(); self.conf.setRange(0, 1); self.conf.setSingleStep(0.01); self.conf.setValue(float(camera_cfg.get("confidence_threshold", 0.25)))
        self.iou = QtWidgets.QDoubleSpinBox(); self.iou.setRange(0, 1); self.iou.setSingleStep(0.01); self.iou.setValue(float(camera_cfg.get("iou_threshold", 0.5)))
        self.frame_skip = QtWidgets.QSpinBox(); self.frame_skip.setRange(1, 30); self.frame_skip.setValue(int(camera_cfg.get("frame_skip", 1)))
        self.imgsz = QtWidgets.QSpinBox(); self.imgsz.setRange(320, 2048); self.imgsz.setSingleStep(32); self.imgsz.setValue(int(camera_cfg.get("imgsz", 640)))
        self.bt_hi = QtWidgets.QDoubleSpinBox(); self.bt_hi.setRange(0, 1); self.bt_hi.setValue(float(camera_cfg.get("bt_track_high_thresh", 0.3)))
        self.bt_lo = QtWidgets.QDoubleSpinBox(); self.bt_lo.setRange(0, 1); self.bt_lo.setValue(float(camera_cfg.get("bt_track_low_thresh", 0.1)))
        self.bt_match = QtWidgets.QDoubleSpinBox(); self.bt_match.setRange(0, 1); self.bt_match.setValue(float(camera_cfg.get("bt_match_thresh", 0.8)))
        self.bt_buffer = QtWidgets.QSpinBox(); self.bt_buffer.setRange(1, 1000); self.bt_buffer.setValue(int(camera_cfg.get("bt_track_buffer", 30)))
        self.crossing = QtWidgets.QLineEdit(str(camera_cfg.get("crossing_judgment_pattern", "line_cross")))
        self.distance_th = QtWidgets.QDoubleSpinBox(); self.distance_th.setRange(1, 1000); self.distance_th.setValue(float(camera_cfg.get("distance_threshold", 25.0)))
        self.cong_interval = QtWidgets.QSpinBox(); self.cong_interval.setRange(1, 60); self.cong_interval.setValue(int(camera_cfg.get("congestion_calculation_interval", 3)))
        self.enable_cong = QtWidgets.QCheckBox("enable_congestion"); self.enable_cong.setChecked(bool(camera_cfg.get("enable_congestion", True)))
        self.spin_threshold = QtWidgets.QDoubleSpinBox(); self.spin_threshold.setRange(0.0, 50.0); self.spin_threshold.setDecimals(2); self.spin_threshold.setSingleStep(0.1); self.spin_threshold.setValue(float(camera_cfg.get("congestion_threshold", 5)))
        self.spin_stay = QtWidgets.QSpinBox(); self.spin_stay.setRange(1, 120); self.spin_stay.setValue(int(camera_cfg.get("long_stay_minutes", 15)))

        ai_form.addRow("yolo_model（未設定時のみ使用）", self.yolo_model)
        ai_form.addRow("confidence_threshold", self.conf)
        ai_form.addRow("iou_threshold", self.iou)
        ai_form.addRow("frame_skip", self.frame_skip)
        ai_form.addRow("imgsz", self.imgsz)
        ai_form.addRow("bt_track_high_thresh", self.bt_hi)
        ai_form.addRow("bt_track_low_thresh", self.bt_lo)
        ai_form.addRow("bt_match_thresh", self.bt_match)
        ai_form.addRow("bt_track_buffer", self.bt_buffer)
        ai_form.addRow("crossing_judgment_pattern", self.crossing)
        ai_form.addRow("distance_threshold", self.distance_th)
        ai_form.addRow("congestion_calculation_interval", self.cong_interval)
        ai_form.addRow(self.enable_cong)
        ai_form.addRow("渋滞指数閾値", self.spin_threshold)
        ai_form.addRow("長時間滞在(分)", self.spin_stay)

        class_group = QtWidgets.QGroupBox("対象クラス (0-7)")
        class_layout = QtWidgets.QGridLayout(class_group)
        self.class_checks: dict[int, QtWidgets.QCheckBox] = {}
        selected = set(int(x) for x in camera_cfg.get("target_classes", [2, 3, 5, 7]))
        for i in range(8):
            cb = QtWidgets.QCheckBox(f"{CLASS_MAP[i]}({i})")
            cb.setChecked(i in selected)
            self.class_checks[i] = cb
            class_layout.addWidget(cb, i // 4, i % 4)
        ai_form.addRow(class_group)

        self.image = ClickableImageLabel("snapshot")
        self.image.setMinimumHeight(340)
        self.image.setStyleSheet("background:#0c0f16;border:1px solid #00D7FF;")
        self.image.point_clicked.connect(self._on_click)
        root.addWidget(self.image)

        self.mode_label = QtWidgets.QLabel("")
        self.mode_label.setStyleSheet("color:#d4e6ff;font-weight:bold;")
        root.addWidget(self.mode_label)

        row = QtWidgets.QHBoxLayout()
        btn_line = QtWidgets.QPushButton("ライン設定")
        btn_line.clicked.connect(lambda: self._set_mode("line"))
        btn_poly = QtWidgets.QPushButton("除外エリア設定")
        btn_poly.clicked.connect(lambda: self._set_mode("poly"))
        btn_finish_poly = QtWidgets.QPushButton("指定終了")
        btn_finish_poly.clicked.connect(self._finish_polygon)
        btn_reset = QtWidgets.QPushButton("やり直し")
        btn_reset.clicked.connect(self._reset_mode)
        row.addWidget(btn_line); row.addWidget(btn_poly); row.addWidget(btn_finish_poly); row.addWidget(btn_reset)
        root.addLayout(row)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._validate_and_accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self.snapshot = latest_frame
        if self.snapshot is None:
            self.snapshot = np.zeros((360, 640, 3), dtype=np.uint8)
            cv2.putText(self.snapshot, "No live frame", (30, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        self._update_mode_label()
        self._render_snapshot()

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        if mode == "line":
            QtWidgets.QMessageBox.information(self, "ライン設定", "ライン設定は2点を取り直します。")
        self._update_mode_label()

    def _update_mode_label(self) -> None:
        txt = "現在モード: ライン設定" if self.mode == "line" else "現在モード: 除外エリア設定"
        self.mode_label.setText(txt)

    def _on_click(self, x: int, y: int) -> None:
        if self.mode == "line":
            if len(self.line_points) >= 2:
                self.line_points = []
            self.line_points.append([x, y])
        else:
            self.exclude_polygon.append([x, y])
        self._render_snapshot()

    def _finish_polygon(self) -> None:
        if len(self.exclude_polygon) < 3:
            QtWidgets.QMessageBox.warning(self, "警告", "除外エリアは3点以上必要です。")
            return
        if has_self_intersection(self.exclude_polygon):
            QtWidgets.QMessageBox.warning(self, "警告", "自己交差ポリゴンは設定できません。やり直してください。")
            self.exclude_polygon = []
        self._render_snapshot()

    def _reset_mode(self) -> None:
        if self.mode == "line":
            self.line_points = []
        else:
            self.exclude_polygon = []
        self._render_snapshot()

    def _render_snapshot(self) -> None:
        frame = self.snapshot.copy()
        if len(self.line_points) == 2:
            cv2.line(frame, tuple(self.line_points[0]), tuple(self.line_points[1]), (0, 255, 255), 2)
            for p in self.line_points:
                cv2.circle(frame, tuple(p), 4, (0, 255, 255), -1)
                cv2.putText(frame, f"{p[0]},{p[1]}", (p[0] + 5, p[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        for i, p in enumerate(self.exclude_polygon):
            cv2.circle(frame, tuple(p), 4, (150, 150, 150), -1)
            cv2.putText(frame, str(i + 1), (p[0] + 5, p[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        if len(self.exclude_polygon) >= 2:
            cv2.polylines(frame, [np.array(self.exclude_polygon, np.int32)], len(self.exclude_polygon) >= 3, (120, 120, 120), 2)

        h, w, _ = frame.shape
        self.image.set_source_size(w, h)
        qimg = QtGui.QImage(frame.data, w, h, frame.strides[0], QtGui.QImage.Format.Format_BGR888)
        self.image.setPixmap(QtGui.QPixmap.fromImage(qimg).scaled(self.image.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio))

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._render_snapshot()

    def _validate_and_accept(self) -> None:
        selected_classes = [i for i, cb in self.class_checks.items() if cb.isChecked()]
        if not selected_classes:
            QtWidgets.QMessageBox.warning(self, "警告", "対象クラスを1つ以上選択してください。")
            return
        if len(self.line_points) != 2:
            QtWidgets.QMessageBox.warning(self, "警告", "ラインは2点指定してください。")
            return
        self.accept()

    def get_updated_config(self) -> dict[str, Any]:
        cfg = dict(self.camera_cfg)
        cfg["camera_name"] = self.name_edit.text().strip() or cfg["camera_name"]
        cfg["stream_url"] = self.url_edit.text().strip()
        cfg["line_start"] = self.line_points[0]
        cfg["line_end"] = self.line_points[1]
        cfg["exclude_polygon"] = self.exclude_polygon
        cfg["yolo_model"] = self.yolo_model.text().strip() or "yolo11m.pt"
        cfg["confidence_threshold"] = float(self.conf.value())
        cfg["iou_threshold"] = float(self.iou.value())
        cfg["frame_skip"] = int(self.frame_skip.value())
        cfg["imgsz"] = int(self.imgsz.value())
        cfg["target_classes"] = [i for i, cb in self.class_checks.items() if cb.isChecked()]
        cfg["bt_track_high_thresh"] = float(self.bt_hi.value())
        cfg["bt_track_low_thresh"] = float(self.bt_lo.value())
        cfg["bt_match_thresh"] = float(self.bt_match.value())
        cfg["bt_track_buffer"] = int(self.bt_buffer.value())
        cfg["crossing_judgment_pattern"] = self.crossing.text().strip() or "line_cross"
        cfg["distance_threshold"] = float(self.distance_th.value())
        cfg["congestion_calculation_interval"] = int(self.cong_interval.value())
        cfg["enable_congestion"] = bool(self.enable_cong.isChecked())
        cfg["congestion_threshold"] = float(self.spin_threshold.value())
        cfg["long_stay_minutes"] = int(self.spin_stay.value())
        return cfg


class CombinedTimelineGraph(QtWidgets.QWidget):
    def __init__(self, mode: str = "line", parent=None):
        super().__init__(parent)
        self.mode = mode
        self.title = ""
        self.today_points: list[tuple[datetime, float]] = []
        self.prev_points: list[tuple[datetime, float]] = []
        self.today_values: list[float] = []
        self.prev_values: list[float] = []
        self.threshold: float | None = None
        self.show_threshold = False
        self.y_min_override: float | None = None
        self.y_max_override: float | None = None
        self.y_axis_labels: dict[float, str] = {}
        self.setFixedHeight(64)

    def set_line_data(self, prev_points: list[tuple[datetime, float]], today_points: list[tuple[datetime, float]], title: str, threshold: float | None = None, show_threshold: bool = True) -> None:
        self.mode = "line"
        self.prev_points = prev_points
        self.today_points = today_points
        self.prev_values = []
        self.today_values = []
        self.title = title
        self.threshold = threshold
        self.show_threshold = show_threshold
        self.update()

    def set_bar_data(self, prev_values: list[int], today_values: list[int], title: str) -> None:
        self.mode = "bar"
        self.prev_values = [float(v) for v in prev_values]
        self.today_values = [float(v) for v in today_values]
        self.prev_points = []
        self.today_points = []
        self.title = title
        self.threshold = None
        self.show_threshold = False
        self.update()

    def set_y_axis_config(self, y_min: float | None = None, y_max: float | None = None, labels: dict[float, str] | None = None) -> None:
        self.y_min_override = y_min
        self.y_max_override = y_max
        self.y_axis_labels = labels or {}
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor("#0f1620"))

        y_axis_w = 28
        right_margin = 5
        top_margin = 18
        bottom_margin = 16
        plot = QtCore.QRectF(y_axis_w, top_margin, max(10, self.width() - y_axis_w - right_margin), max(10, self.height() - top_margin - bottom_margin))

        painter.setPen(QtGui.QPen(QtGui.QColor("#1d6f8b"), 1))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 3, 3)
        painter.drawRect(plot)
        painter.setPen(QtGui.QColor("#cfefff"))
        title_rect = QtCore.QRectF(0, 0, self.width(), 18)
        painter.drawText(
            title_rect,
            QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignVCenter,
            self.title,
        )

        if self.mode == "line":
            ys = [v for _, v in self.prev_points] + [v for _, v in self.today_points]
        else:
            ys = self.prev_values[:] + self.today_values[:]
        if self.show_threshold and self.threshold is not None:
            ys.append(float(self.threshold))
        y_max = float(self.y_max_override) if self.y_max_override is not None else max(1.0, max(ys) if ys else 1.0)
        y_min = float(self.y_min_override) if self.y_min_override is not None else 0.0
        if y_max <= y_min:
            y_max = y_min + 1.0

        if self.y_axis_labels:
            ticks = [(float(v), str(lbl)) for v, lbl in sorted(self.y_axis_labels.items(), key=lambda x: x[0]) if y_min <= float(v) <= y_max]
        else:
            ticks = []
            for i in range(5):
                ratio = i / 4.0
                value = y_min + (y_max - y_min) * ratio
                ticks.append((value, f"{value:.1f}"))

        for value, tick_label in ticks:
            ratio = (value - y_min) / (y_max - y_min)
            y = plot.bottom() - ratio * plot.height()
            painter.setPen(QtGui.QPen(QtGui.QColor("#274457"), 1))
            painter.drawLine(QtCore.QPointF(plot.left(), y), QtCore.QPointF(plot.right(), y))
            painter.setPen(QtGui.QColor("#8db6c7"))
            painter.drawText(2, int(y) + 4, tick_label)

        x_ticks = list(range(25))
        for h in x_ticks:
            x = plot.left() + (h / 24.0) * plot.width()
            painter.setPen(QtGui.QPen(QtGui.QColor("#274457"), 1))
            painter.drawLine(QtCore.QPointF(x, plot.top()), QtCore.QPointF(x, plot.bottom()))
            painter.setPen(QtGui.QColor("#8db6c7"))
            text = f"{h}"
            text_w = painter.fontMetrics().horizontalAdvance(text)
            painter.drawText(int(x - text_w / 2), self.height() - 6, text)

        if self.mode == "line" and (self.prev_points or self.today_points):
            for points, color in [(self.prev_points, QtGui.QColor("#2f7dff")), (self.today_points, QtGui.QColor("#ff3b3b"))]:
                if not points:
                    continue
                path = QtGui.QPainterPath()
                for i, (ts, value) in enumerate(points):
                    sec = ts.hour * 3600 + ts.minute * 60 + ts.second
                    x = plot.left() + (sec / 86400.0) * plot.width()
                    y = plot.bottom() - ((value - y_min) / (y_max - y_min)) * plot.height()
                    if i == 0:
                        path.moveTo(x, y)
                    else:
                        path.lineTo(x, y)
                painter.setPen(QtGui.QPen(color, 2.0))
                painter.drawPath(path)
            if self.show_threshold and self.threshold is not None:
                th_y = plot.bottom() - ((self.threshold - y_min) / (y_max - y_min)) * plot.height()
                painter.setPen(QtGui.QPen(QtGui.QColor("#ffd400"), 2.0))
                painter.drawLine(QtCore.QPointF(plot.left(), th_y), QtCore.QPointF(plot.right(), th_y))
                painter.setPen(QtGui.QColor("#ffd400"))
                painter.drawText(int(plot.left()) + 6, int(plot.top()) + 12, f"TH={self.threshold:.2f}")
        elif self.mode == "bar" and (self.prev_values or self.today_values):
            n = max(1, len(self.prev_values), len(self.today_values))
            slot_w = plot.width() / n
            bar_w = max(1.8, slot_w * 0.38)
            for i in range(n):
                prev_val = self.prev_values[i] if i < len(self.prev_values) else 0.0
                today_val = self.today_values[i] if i < len(self.today_values) else 0.0
                center_x = plot.left() + (i + 0.5) * slot_w
                prev_h = ((prev_val - y_min) / (y_max - y_min)) * plot.height()
                today_h = ((today_val - y_min) / (y_max - y_min)) * plot.height()
                offset = min(slot_w * 0.22, max(2.0, bar_w * 0.7))
                prev_x = center_x - offset - bar_w / 2
                today_x = center_x + offset - bar_w / 2
                painter.fillRect(QtCore.QRectF(prev_x, plot.bottom() - prev_h, bar_w, prev_h), QtGui.QColor("#2f7dff"))
                painter.fillRect(QtCore.QRectF(today_x, plot.bottom() - today_h, bar_w, today_h), QtGui.QColor("#ff3b3b"))

        self._draw_legend(painter, plot)

    def _draw_legend(self, painter: QtGui.QPainter, plot: QtCore.QRectF) -> None:
        legend = [("前日", QtGui.QColor("#2f7dff")), ("当日", QtGui.QColor("#ff3b3b"))]
        if self.mode == "line" and self.show_threshold:
            legend.append(("閾値", QtGui.QColor("#ffd400")))
        y = int(plot.top()) + 10
        x = int(plot.right()) - 8
        for label, color in reversed(legend):
            text_w = painter.fontMetrics().horizontalAdvance(label)
            x -= text_w
            painter.setPen(QtGui.QColor("#d9ecff"))
            painter.drawText(x, y + 4, label)
            x -= 16
            painter.setPen(QtGui.QPen(color, 2))
            painter.drawLine(x, y, x + 12, y)
            x -= 10


class CongestionIndexBar(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.score = 0.0
        self.threshold = 5.0
        self.setMinimumHeight(30)
        self.setMaximumHeight(30)

    def set_values(self, score: float, threshold: float) -> None:
        self.score = float(score)
        self.threshold = float(threshold)
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QtGui.QPen(QtGui.QColor("#1f4f7a"), 1))
        painter.setBrush(QtGui.QColor("#0c1a24"))
        painter.drawRoundedRect(rect, 4, 4)

        score_limited = max(0.0, min(20.0, self.score))
        threshold_limited = max(0.0, min(20.0, self.threshold))
        ratio = score_limited / 20.0
        fill = QtCore.QRectF(rect.left(), rect.top(), rect.width() * ratio, rect.height())
        bar_color = QtGui.QColor("#ff3b3b" if self.score >= self.threshold else "#2f7dff")
        painter.fillRect(fill, bar_color)

        th_x = rect.left() + rect.width() * (threshold_limited / 20.0)
        painter.setPen(QtGui.QPen(QtGui.QColor("#ffd400"), 2))
        painter.drawLine(QtCore.QPointF(th_x, rect.top()), QtCore.QPointF(th_x, rect.bottom()))

        painter.setPen(QtGui.QColor("#ffffff"))
        font = painter.font()
        font.setPointSizeF(max(12.0, font.pointSizeF() * 1.2))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, f"渋滞指数 {self.score:.1f}")


class CameraPanel(QtWidgets.QFrame):
    line_setting_requested = QtCore.pyqtSignal(int)
    exclude_setting_requested = QtCore.pyqtSignal(int)
    camera_setting_requested = QtCore.pyqtSignal(int)
    threshold_changed = QtCore.pyqtSignal(int, float)

    def __init__(self, camera_cfg: dict[str, Any], parent=None):
        super().__init__(parent)
        self.camera_cfg = camera_cfg
        self.camera_id = camera_cfg["camera_id"]
        self._latest_pixmap: QtGui.QPixmap | None = None
        self._last_frame_size: tuple[int | None, int | None] = (None, None)
        self._status_connected = False
        self.video_target_w = 576
        self.video_target_h = 324
        self.setStyleSheet("QFrame{background:#0a0e13;border:1px solid #169db8;border-radius:6px;} QLabel{color:#cfefff;}")
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(3)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(1068)
        self.setMaximumWidth(1068)

        top_row = QtWidgets.QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)

        video_box = QtWidgets.QWidget()
        video_layout = QtWidgets.QVBoxLayout(video_box)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(2)
        video_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.video = QtWidgets.QLabel("video")
        self.video.setFixedSize(576, 324)
        self.video.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.video.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.video.setStyleSheet("background:#010203;border:1px solid #00a6d6;")
        video_layout.addWidget(self.video, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        video_box.setFixedSize(576, 324)
        video_box.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        top_row.addWidget(video_box, 0, QtCore.Qt.AlignmentFlag.AlignTop)

        right_box = QtWidgets.QWidget()
        right_box.setFixedWidth(460)
        right_box.setFixedHeight(324)
        right_box.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        right = QtWidgets.QVBoxLayout(right_box)
        right.setContentsMargins(3, 3, 3, 3)
        right.setSpacing(2)
        right.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        role_map = {
            2: "KING",
            1: "QUEEN",
            3: "JACK",
        }
        role_style_map = {
            2: "background:#f4c542;color:#1a1200;border-radius:8px;font-weight:900;font-size:18px;padding:2px 8px;",
            1: "background:#d94b70;color:#ffffff;border-radius:8px;font-weight:900;font-size:18px;padding:2px 8px;",
            3: "background:#3cbf6b;color:#08150d;border-radius:8px;font-weight:900;font-size:18px;padding:2px 8px;",
        }
        self.role_badge = QtWidgets.QLabel(role_map.get(self.camera_id, "ROOK"))
        self.role_badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.role_badge.setFixedHeight(28)
        self.role_badge.setStyleSheet(
            role_style_map.get(
                self.camera_id,
                "background:#7fd0ff;color:#000000;border-radius:8px;font-weight:900;font-size:18px;padding:2px 8px;",
            )
        )
        right.addWidget(self.role_badge, 0, QtCore.Qt.AlignmentFlag.AlignTop)

        self.title = QtWidgets.QLabel("")
        self.title.setStyleSheet("font-size:13px;color:#00D7FF;font-weight:bold;line-height:1.1em;")
        self.title.setWordWrap(False)
        self.title.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        right.addWidget(self.title, 0, QtCore.Qt.AlignmentFlag.AlignTop)

        self.stream_meta = QtWidgets.QLabel("入力画像サイズ：-- ｜ FPS：  0.0 ｜ 更新：--:--:--")
        self.stream_meta.setWordWrap(False)
        mono_font = QtGui.QFont("Consolas")
        mono_font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
        self.stream_meta.setFont(mono_font)
        self.stream_meta.setStyleSheet(
            "font-size:12px;color:#9edff6;background:#08121b;border:1px solid #1f4f7a;border-radius:6px;padding:4px;line-height:1.1em;"
        )
        right.addWidget(self.stream_meta)

        btn_col = QtWidgets.QVBoxLayout()
        btn_col.setSpacing(4)
        self.btn_line = QtWidgets.QPushButton("ライン設定")
        self.btn_line.clicked.connect(lambda: self.line_setting_requested.emit(self.camera_id))
        self.btn_exclude = QtWidgets.QPushButton("除外エリア")
        self.btn_exclude.clicked.connect(lambda: self.exclude_setting_requested.emit(self.camera_id))
        self.btn_ai = QtWidgets.QPushButton("解析条件")
        self.btn_ai.clicked.connect(lambda: self.camera_setting_requested.emit(self.camera_id))
        for btn in (self.btn_line, self.btn_exclude, self.btn_ai):
            btn.setFixedHeight(20)
            btn.setStyleSheet("font-size:10px;padding:2px;")
            btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
            btn.setFixedWidth(92)
            btn_col.addWidget(btn)

        self.congestion_bar = CongestionIndexBar()

        level_row = QtWidgets.QHBoxLayout()
        level_row.setContentsMargins(0, 0, 0, 0)
        level_row.setSpacing(4)

        level_row.addWidget(self.congestion_bar, 2)

        th_box = QtWidgets.QWidget()
        th_box_layout = QtWidgets.QHBoxLayout(th_box)
        th_box_layout.setContentsMargins(0, 0, 0, 0)
        th_box_layout.setSpacing(3)
        th_label = QtWidgets.QLabel("しきい値")
        th_label.setStyleSheet("font-size:12px;color:#00d9ff;font-weight:bold;")
        self.threshold_edit = QtWidgets.QLineEdit()
        self.threshold_edit.setStyleSheet("QLineEdit{background:#08121c;color:#8ff6ff;border:1px solid #00a9d6;padding:2px 6px;border-radius:4px;font-weight:bold;}")
        self.threshold_edit.setValidator(QtGui.QDoubleValidator(0.0, 20.0, 1, self.threshold_edit))
        self.threshold_edit.setPlaceholderText("0.0-20.0")
        self.threshold_edit.returnPressed.connect(self._on_threshold_enter_pressed)
        th_box_layout.addWidget(th_label)
        th_box_layout.addWidget(self.threshold_edit, 1)
        level_row.addWidget(th_box, 1)
        right.addLayout(level_row)

        self.wakimura_label = QtWidgets.QLabel("脇村指標 α：--")
        self.wakimura_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.wakimura_label.setStyleSheet(
            "font-size:11px;"
            "font-weight:bold;"
            "background:#09131d;"
            "border:1px solid #6a4cff;"
            "color:#d8c8ff;"
            "border-radius:6px;"
            "padding:2px 4px;"
        )
        right.addWidget(self.wakimura_label)

        metric_head = QtWidgets.QLabel("脇村モデル主要値")
        metric_head.setStyleSheet("font-size:11px;font-weight:bold;color:#c5bdff;padding:0px;margin:0px;")
        right.addWidget(metric_head)
        self.wakimura_grid = QtWidgets.QGridLayout()
        self.wakimura_grid.setContentsMargins(0, 0, 0, 0)
        self.wakimura_grid.setHorizontalSpacing(3)
        self.wakimura_grid.setVerticalSpacing(3)
        self.wakimura_grid_widget = QtWidgets.QWidget()
        self.wakimura_grid_widget.setLayout(self.wakimura_grid)
        right.addWidget(self.wakimura_grid_widget)
        self.wakimura_value_labels: dict[str, QtWidgets.QLabel] = {}
        self._build_wakimura_cards()

        count_row = QtWidgets.QHBoxLayout()
        count_row.setContentsMargins(0, 0, 0, 0)
        count_row.setSpacing(4)
        self.ltor_card = QtWidgets.QLabel("LtoR：0")
        self.rtol_card = QtWidgets.QLabel("RtoL：0")
        for card in (self.ltor_card, self.rtol_card):
            card.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            card.setStyleSheet("font-size:14px;font-weight:900;background:#071925;border:1px solid #14b6dc;color:#98f5ff;border-radius:6px;padding:1px;")
            count_row.addWidget(card, 1)
        right.addLayout(count_row)

        stay_head = QtWidgets.QLabel("1分以上滞在ID")
        stay_head.setStyleSheet("font-size:11px;font-weight:bold;color:#9de7ff;padding:0px;margin:0px;")
        right.addWidget(stay_head)
        self.stay_grid = QtWidgets.QGridLayout()
        self.stay_grid.setContentsMargins(0, 0, 0, 0)
        self.stay_grid.setHorizontalSpacing(1)
        self.stay_grid.setVerticalSpacing(1)
        self.stay_grid.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self.stay_box = QtWidgets.QWidget()
        self.stay_box.setLayout(self.stay_grid)
        self.stay_box.setStyleSheet("background:#07131f;border:1px solid #145c7a;border-radius:6px;")
        self.stay_box.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Minimum)
        right.addWidget(self.stay_box)
        self._render_stay_cards([])

        top_row.addWidget(right_box, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        root.addLayout(top_row)

        self.graphs: list[CombinedTimelineGraph] = []
        graphs_box = QtWidgets.QWidget()
        graphs_layout = QtWidgets.QVBoxLayout(graphs_box)
        graphs_layout.setContentsMargins(0, 0, 0, 0)
        graphs_layout.setSpacing(2)
        for _ in range(3):
            g = CombinedTimelineGraph("line")
            g.setFixedHeight(62)
            self.graphs.append(g)
            graphs_layout.addWidget(g)
        root.addWidget(graphs_box)
        self._update_title()
        self.threshold_edit.setText(f"{float(self.camera_cfg.get('congestion_threshold', 5.0)):.1f}")

    def update_view(self, payload: dict[str, Any]) -> None:
        frame = payload.get("frame")
        if frame is not None:
            h, w, _ = frame.shape
            src_w = payload.get("source_frame_width")
            src_h = payload.get("source_frame_height")
            if src_w and src_h:
                self._last_frame_size = (int(src_w), int(src_h))
            qimg = QtGui.QImage(frame.data, w, h, frame.strides[0], QtGui.QImage.Format.Format_BGR888)
            self._latest_pixmap = QtGui.QPixmap.fromImage(qimg)
            self._update_video_pixmap()

        score = float(payload.get("congestion_score", 0))
        threshold = float(payload.get("threshold", 5))
        status_raw = str(payload.get("status", ""))
        self._status_connected = status_raw == "RUNNING"

        ltor = payload.get("pass_bins_ltor", [0] * 144)
        rtol = payload.get("pass_bins_rtol", [0] * 144)
        self._update_title(payload.get("camera_name"), payload.get("stream_name"))
        if not self.threshold_edit.hasFocus():
            self.threshold_edit.setText(f"{threshold:.1f}")
        self._update_count_cards(int(sum(ltor)), int(sum(rtol)))
        self._render_stay_cards(payload.get("long_stay_list", []))
        self._update_congestion_bar(score, threshold)
        wak_alpha = float(payload.get("wakimura_alpha", 0.0))
        wak_mode = bool(payload.get("wakimura_high_load_mode", False))
        self.wakimura_label.setText(f"脇村指標 α：{wak_alpha:.3f} [{'HL' if wak_mode else 'WIN'}]")
        self._update_stream_meta(float(payload.get("fps", 0.0)))
        self._update_wakimura_cards(payload)
        self.graphs[0].set_line_data(payload.get("prev_congestion_points", []), payload.get("congestion_points", []), "渋滞指数", threshold=threshold, show_threshold=True)
        self.graphs[1].set_bar_data(payload.get("hist_prev_ltor", [0] * 144), ltor, "LtoR")
        self.graphs[2].set_bar_data(payload.get("hist_prev_rtol", [0] * 144), rtol, "RtoL")

    def _update_congestion_bar(self, score: float, threshold: float) -> None:
        self.congestion_bar.set_values(score, threshold)

    def set_status(self, status_text: str) -> None:
        self._status_connected = status_text == "RUNNING"
        self._update_title()

    def _update_video_pixmap(self) -> None:
        if self._latest_pixmap is None:
            return
        scaled = self._latest_pixmap.scaled(
            self.video.size(),
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        self.video.setPixmap(scaled)

    def _update_title(self, camera_name: str | None = None, stream_name: str | None = None) -> None:
        cam_name = camera_name or self.camera_cfg.get("camera_name", f"Camera{self.camera_id}")
        raw_stream = stream_name or self.camera_cfg.get("stream_name") or self.camera_cfg.get("stream_url")
        stream = str(raw_stream).strip() if raw_stream else f"stream{self.camera_id}"
        status_text = "受信中" if self._status_connected else "通信無し"
        title_text = f"{cam_name} ｜ {stream} ｜ {status_text}"
        if self.title.width() > 8:
            metrics = self.title.fontMetrics()
            title_text = metrics.elidedText(title_text, QtCore.Qt.TextElideMode.ElideRight, self.title.width())
        self.title.setText(title_text)

    def _update_stream_meta(self, fps: float) -> None:
        w, h = self._last_frame_size
        res_text = f"{w}×{h}" if w and h else "--"
        ts_text = datetime.now().strftime("%H:%M:%S")
        fps_text = f"{fps:5.1f}"
        self.stream_meta.setText(f"入力画像サイズ：{res_text:<9} ｜ FPS：{fps_text} ｜ 更新：{ts_text}")

    def _on_threshold_enter_pressed(self) -> None:
        current = float(self.camera_cfg.get("congestion_threshold", 5.0))
        text = self.threshold_edit.text().strip()
        if not text:
            self.threshold_edit.setText(f"{current:.1f}")
            return
        try:
            value = float(text)
        except ValueError:
            self.threshold_edit.setText(f"{current:.1f}")
            return
        value = max(0.0, min(20.0, value))
        self.camera_cfg["congestion_threshold"] = float(value)
        self.threshold_edit.setText(f"{value:.1f}")
        self._update_congestion_bar(self.congestion_bar.score, float(value))
        self.threshold_changed.emit(self.camera_id, float(value))

    def _update_count_cards(self, ltor_total: int, rtol_total: int) -> None:
        self.ltor_card.setText(f"LtoR：{ltor_total}")
        self.rtol_card.setText(f"RtoL：{rtol_total}")

    def _render_stay_cards(self, entries: list[list[float] | tuple[int, float]]) -> None:
        self._clear_layout(self.stay_grid)
        if not entries:
            empty_label = QtWidgets.QLabel("該当なし")
            empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            empty_label.setStyleSheet("font-size:9px;color:#5fa2b8;padding:0px;")
            empty_label.setFixedWidth(40)
            empty_label.setFixedHeight(28)
            self.stay_grid.addWidget(empty_label, 0, 0)
            self.stay_box.setFixedHeight(32)
            return
        visible_entries = entries[:14]
        for idx, item in enumerate(visible_entries):
            track_id = int(item[0])
            stay_mins = float(item[1])
            stay_mins_int = max(0, int(stay_mins))
            border_color = "#16b8d8"
            if stay_mins_int >= 20:
                border_color = "#ff4d4d"
            elif stay_mins_int >= 10:
                border_color = "#ffe066"
            card = QtWidgets.QLabel(f"{track_id:03d}\n{stay_mins_int:d}m")
            card.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            card.setStyleSheet(
                "font-size:9px;"
                "background:#061726;"
                f"border:1px solid {border_color};"
                "border-radius:5px;color:#95f6ff;padding:0px;font-weight:bold;line-height:1.05em;"
            )
            card.setFixedWidth(40)
            card.setFixedHeight(28)
            self.stay_grid.addWidget(card, idx // 7, idx % 7)
        self.stay_box.setFixedHeight(58 if len(visible_entries) > 7 else 32)

    def _build_wakimura_cards(self) -> None:
        labels = [
            ("停止台数", "wak_stop_count"),
            ("低速台数", "wak_slow_count"),
            ("総追跡台数", "wak_total_tracks"),
            ("平均移動量", "wak_avg_move"),
            ("中央移動量", "wak_median_move"),
            ("停止率", "wak_stop_ratio"),
            ("低速率", "wak_slow_ratio"),
            ("score合計", "wak_score_sum"),
            ("score平均", "wak_score_avg"),
            ("1分以上滞在台数", "wak_stay_over_1min"),
            ("最大滞在時間", "wak_max_stay_min"),
            ("窓内評価台数", "wak_window_count"),
        ]
        for idx, (title, key) in enumerate(labels):
            card = QtWidgets.QFrame()
            card.setFixedSize(70, 34)
            card.setStyleSheet("background:#101226;border:1px solid #6a4cff;border-radius:6px;")
            card_layout = QtWidgets.QVBoxLayout(card)
            card_layout.setContentsMargins(3, 1, 3, 1)
            card_layout.setSpacing(1)
            t = QtWidgets.QLabel(title)
            t.setStyleSheet("font-size:10px;color:#b7a6ff;font-weight:bold;")
            v = QtWidgets.QLabel("--")
            v.setStyleSheet("font-size:10px;color:#efe7ff;font-weight:bold;")
            v.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            card_layout.addWidget(t)
            card_layout.addWidget(v)
            self.wakimura_grid.addWidget(card, idx // 6, idx % 6)
            self.wakimura_value_labels[key] = v

    def _update_wakimura_cards(self, payload: dict[str, Any]) -> None:
        for key, label in self.wakimura_value_labels.items():
            val = payload.get(key, "--")
            if isinstance(val, float):
                if "ratio" in key:
                    text = f"{val * 100:.1f}%"
                else:
                    text = f"{val:.3f}" if abs(val) < 100 else f"{val:.1f}"
            else:
                text = str(val)
            if label.text() != text:
                label.setText(text)

    def _clear_layout(self, layout: QtWidgets.QLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if child_layout is not None:
                self._clear_layout(child_layout)
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()


def resolve_model_path(camera_cfg: dict[str, Any], system_cfg: dict[str, Any], root_dir: Path) -> Path:
    raw = system_cfg.get("model_path") or system_cfg.get("yolo_model") or system_cfg.get("YOLO_MODEL") or camera_cfg.get("yolo_model") or "yolo11m.pt"
    if not raw:
        raise FileNotFoundError("YOLO model is not configured. Set yolo_model / model_path in config.")
    p = Path(str(raw))
    if not p.is_absolute():
        candidates = [root_dir / p, root_dir / "models" / p, Path.cwd() / p]
        for c in candidates:
            if c.exists():
                return c
        raise FileNotFoundError(
            f"YOLO model file not found: {raw}. "
            "Place the model locally under 04_ai_monitor or 04_ai_monitor/models."
        )
    if not p.exists():
        raise FileNotFoundError(f"YOLO model file not found: {p}")
    return p


# =========================================
# Camera Worker
# =========================================
class CameraWorker(QtCore.QObject):
    frame_ready = QtCore.pyqtSignal(dict)
    status_changed = QtCore.pyqtSignal(int, str)
    error_occurred = QtCore.pyqtSignal(int, str)
    finished = QtCore.pyqtSignal(int)

    def __init__(self, camera_cfg: dict[str, Any], system_cfg: dict[str, Any], root_dir: Path):
        super().__init__()
        self.camera_cfg = camera_cfg
        self.system_cfg = system_cfg
        self.root_dir = root_dir
        self.camera_id = int(camera_cfg["camera_id"])
        self.camera_name = camera_cfg["camera_name"]

        self.device = self._resolve_device(system_cfg.get("device_preference", "auto"))
        self.gpu_name = torch.cuda.get_device_name(0) if self.device.startswith("cuda") else "CPU"

        self.model_path = resolve_model_path(self.camera_cfg, self.system_cfg, self.root_dir)
        try:
            self.model = YOLO(str(self.model_path))
        except Exception as exc:
            raise RuntimeError(f"[ERROR] cam{self.camera_id} model load failed: {exc}") from exc
        self.target_classes = set(int(x) for x in self.camera_cfg.get("target_classes", [2, 3, 5, 7]))

        line = [self.camera_cfg.get("line_start", [0, 0]), self.camera_cfg.get("line_end", [100, 0])]
        self.counter = LineCounter(line)
        self.congestion = CongestionScorer(
            int(self.camera_cfg.get("congestion_calculation_interval", 3)),
            smoothing_window=int(self.system_cfg.get("congestion_smoothing_window", 6)),
        )
        # 脇村指標 α は参考表示専用で、既存 LEVEL 判定ロジックには一切使用しない。
        # 算出ロジックは app/90_sample/20_rotary_efficiency_analysis.py の考え方を監視向けに簡略適用。
        self.wakimura_alpha = WakimuraAlphaCalculator(
            rotary_capacity=int(self.camera_cfg.get("wakimura_rotary_capacity", 10)),
            base_stay_time_sec=float(self.camera_cfg.get("wakimura_base_stay_time_sec", 60.0)),
            window_seconds=int(self.camera_cfg.get("wakimura_window_seconds", 300)),
            high_load_vehicle_threshold=int(self.camera_cfg.get("wakimura_high_load_vehicle_threshold", 7)),
        )
        self.track_state = TrackState()
        self.track_class_memory: dict[int, dict[str, Any]] = {}
        self.display_id_map: dict[int, int] = {}
        self.display_id_counter = 1
        self.cross_flash_frames: dict[int, int] = {}

        self.cap = None
        self.last_frame = None
        self.last_raw_frame = None
        self.fps = 0.0
        self.frame_index = 0
        self._running = True
        self._next_reconnect_time = 0.0
        self._next_infer_retry_time = 0.0
        self._last_warn_emit = 0.0
        self._status_text = "INIT"
        self.display_scale = float(self.camera_cfg.get("display_scale", 0.8))
        self.csv_error_state = {"congestion": False, "pass": False, "long_stay": False}
        self._csv_error_count = {"congestion": 0, "pass": 0, "long_stay": 0}
        self.read_fail_count = 0
        self.max_read_fail_before_reconnect = 5
        self.last_reconnect_at = 0.0
        self.reconnect_fail_count = 0

        self.today = datetime.now().date()
        self.metrics_dir = root_dir / "data" / "metrics" / f"cam{self.camera_id}"
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        self.congestion_csv, self.pass_csv, self.long_stay_csv = self._ensure_daily_csvs()
        self.previous_day_hist_ltor, self.previous_day_hist_rtol = self._load_previous_day_histogram()
        self.previous_day_congestion_points = self._load_previous_day_congestion_points()
        today_ltor, today_rtol = self._load_today_pass_histogram()
        self.counter.state.pass_bins_ltor = today_ltor
        self.counter.state.pass_bins_rtol = today_rtol
        today_points = self._load_today_congestion_points()
        self.congestion.state.frame_time_stamps = [ts for ts, _ in today_points]
        self.congestion.state.frame_motion_scores = [v for _, v in today_points]
        self.congestion.state.smoothed_motion_scores = []
        for _, score in today_points:
            self.congestion.smoother.add(float(score))
            self.congestion.state.smoothed_motion_scores.append(self.congestion.smoother.current())
        if today_points:
            self.congestion.state.current_congestion_index = today_points[-1][1]
            self.congestion.state.current_smoothed_index = self.congestion.state.smoothed_motion_scores[-1]

    def get_latest_raw_frame(self):
        return None if self.last_raw_frame is None else self.last_raw_frame.copy()

    def update_camera_config(self, new_cfg: dict[str, Any]) -> None:
        prev_cfg = dict(self.camera_cfg)
        old_model_path = self.model_path
        self.camera_cfg.update(new_cfg)
        self.camera_name = new_cfg.get("camera_name", self.camera_name)
        self.target_classes = set(int(x) for x in new_cfg.get("target_classes", [2, 3, 5, 7]))
        self.counter.update_line([new_cfg.get("line_start", [0, 0]), new_cfg.get("line_end", [100, 0])])
        self.congestion.update_interval(int(new_cfg.get("congestion_calculation_interval", 3)))
        self.congestion.update_smoothing_window(int(self.system_cfg.get("congestion_smoothing_window", 6)))
        self.display_scale = float(self.camera_cfg.get("display_scale", 0.8))

        new_model_path = resolve_model_path(self.camera_cfg, self.system_cfg, self.root_dir)
        if new_model_path != old_model_path:
            try:
                new_model = YOLO(str(new_model_path))
                self.model = new_model
                self.model_path = new_model_path
            except Exception as exc:
                self.camera_cfg = prev_cfg
                raise RuntimeError(f"[ERROR] cam{self.camera_id} model load failed: {exc}") from exc

    def _resolve_device(self, preference: str) -> str:
        if preference == "cpu":
            return "cpu"
        if torch.cuda.is_available():
            return "cuda:0"
        return "cpu"

    def _stream_source(self):
        stream_url = str(self.camera_cfg.get("stream_url", "0"))
        return int(stream_url) if stream_url.isdigit() else stream_url

    def connect(self) -> None:
        self._release_capture()
        src = self._stream_source()
        try:
            self.cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        except Exception:
            self.cap = cv2.VideoCapture(src)
        if self.cap is not None:
            try:
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
        self.read_fail_count = 0

    def _reconnect_capture(self, reason: str = "") -> None:
        now_ts = time.time()
        reconnect_sec = float(self.camera_cfg.get("reconnect_sec", 3))
        if now_ts - self.last_reconnect_at < reconnect_sec:
            return
        self.last_reconnect_at = now_ts
        self._set_status("RECONNECTING")
        self.error_occurred.emit(self.camera_id, f"[INFO] cam{self.camera_id} reconnect start: {reason or '-'}")
        self._release_capture()
        time.sleep(min(1.0, reconnect_sec))
        try:
            self.connect()
            self._set_status("RUNNING")
            self.reconnect_fail_count = 0
            self.error_occurred.emit(self.camera_id, f"[INFO] cam{self.camera_id} reconnect success")
        except Exception as exc:
            self.reconnect_fail_count += 1
            if self.reconnect_fail_count <= 3 or self.reconnect_fail_count % 10 == 0:
                self.error_occurred.emit(self.camera_id, f"[WARN] cam{self.camera_id} reconnect failed({self.reconnect_fail_count}): {exc}")

    def _release_capture(self) -> None:
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            finally:
                self.cap = None

    def _set_status(self, text: str) -> None:
        if self._status_text == text:
            return
        self._status_text = text
        self.status_changed.emit(self.camera_id, text)

    def _in_polygon(self, point: tuple[float, float], polygon_points: list[list[int]]) -> bool:
        if not polygon_points:
            return False
        poly = np.array(polygon_points, np.int32)
        return cv2.pointPolygonTest(poly, point, False) >= 0

    def _ensure_daily_csvs(self):
        date_str = self.today.strftime("%Y-%m-%d")
        congestion_ts = self.metrics_dir / f"congestion_timeseries_{date_str}.csv"
        pass_events = self.metrics_dir / f"pass_events_{date_str}.csv"
        long_stay = self.metrics_dir / f"long_stay_events_{date_str}.csv"

        self._ensure_csv_header(
            congestion_ts,
            ["timestamp", "camera_id", "camera_name", "congestion_score", "congestion_threshold", "threshold_over", "fps"],
        )
        self._ensure_csv_header(pass_events, ["timestamp", "camera_id", "track_id", "class_name", "direction"])
        self._ensure_csv_header(long_stay, ["first_seen", "detected_at", "camera_id", "track_id", "stay_minutes", "class_name"])
        return congestion_ts, pass_events, long_stay

    @staticmethod
    def _ensure_csv_header(path: Path, header: list[str]) -> None:
        if path.exists():
            return
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(header)

    def _append_csv(self, path: Path, row: list[Any]) -> None:
        with path.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)

    def _append_csv_safe(self, path: Path, row: list[Any], kind: str) -> None:
        try:
            self._append_csv(path, row)
            if self.csv_error_state.get(kind, False):
                self.error_occurred.emit(self.camera_id, f"[INFO] cam{self.camera_id} {kind} csv write recovered")
            self.csv_error_state[kind] = False
            self._csv_error_count[kind] = 0
        except PermissionError as exc:
            self._csv_error_count[kind] = self._csv_error_count.get(kind, 0) + 1
            count = self._csv_error_count[kind]
            if not self.csv_error_state.get(kind, False) or count % 20 == 0:
                self.error_occurred.emit(
                    self.camera_id,
                    f"[WARN] cam{self.camera_id} {kind} csv is locked ({count}): {exc}",
                )
            self.csv_error_state[kind] = True
        except Exception as exc:
            self._csv_error_count[kind] = self._csv_error_count.get(kind, 0) + 1
            count = self._csv_error_count[kind]
            if not self.csv_error_state.get(kind, False) or count % 20 == 0:
                self.error_occurred.emit(self.camera_id, f"[WARN] cam{self.camera_id} {kind} csv write failed ({count}): {exc}")
            self.csv_error_state[kind] = True

    def _load_previous_day_histogram(self) -> tuple[list[int], list[int]]:
        prev = self.today.fromordinal(self.today.toordinal() - 1)
        prev_file = self.metrics_dir / f"pass_events_{prev.strftime('%Y-%m-%d')}.csv"
        ltor = [0] * 144
        rtol = [0] * 144
        if not prev_file.exists():
            return ltor, rtol
        with prev_file.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                dt = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                idx = (dt.hour * 60 + dt.minute) // 10
                if 0 <= idx < 144:
                    if row.get("direction") == "LtoR":
                        ltor[idx] += 1
                    else:
                        rtol[idx] += 1
        return ltor, rtol

    def _load_previous_day_congestion_points(self) -> list[tuple[datetime, float]]:
        prev = self.today.fromordinal(self.today.toordinal() - 1)
        prev_file = self.metrics_dir / f"congestion_timeseries_{prev.strftime('%Y-%m-%d')}.csv"
        points: list[tuple[datetime, float]] = []
        if not prev_file.exists():
            return points
        with prev_file.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    score = float(row.get("congestion_score", 0.0))
                    points.append((ts, score))
                except Exception:
                    continue
        return points

    def _load_today_pass_histogram(self) -> tuple[list[int], list[int]]:
        today_file = self.metrics_dir / f"pass_events_{self.today.strftime('%Y-%m-%d')}.csv"
        ltor = [0] * 144
        rtol = [0] * 144
        if not today_file.exists():
            return ltor, rtol
        with today_file.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    dt = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                idx = (dt.hour * 60 + dt.minute) // 10
                if 0 <= idx < 144:
                    if row.get("direction") == "LtoR":
                        ltor[idx] += 1
                    else:
                        rtol[idx] += 1
        return ltor, rtol

    def _load_today_congestion_points(self) -> list[tuple[datetime, float]]:
        today_file = self.metrics_dir / f"congestion_timeseries_{self.today.strftime('%Y-%m-%d')}.csv"
        points: list[tuple[datetime, float]] = []
        if not today_file.exists():
            return points
        with today_file.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    points.append((datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S"), float(row["congestion_score"])))
                except Exception:
                    continue
        return points

    def _rollover_if_needed(self, now: datetime) -> None:
        if now.date() == self.today:
            return
        self.today = now.date()
        self.display_id_map.clear()
        self.display_id_counter = 1
        self.cross_flash_frames.clear()
        self.congestion_csv, self.pass_csv, self.long_stay_csv = self._ensure_daily_csvs()
        self.previous_day_hist_ltor, self.previous_day_hist_rtol = self._load_previous_day_histogram()
        self.previous_day_congestion_points = self._load_previous_day_congestion_points()
        self.counter.state.pass_bins_ltor, self.counter.state.pass_bins_rtol = self._load_today_pass_histogram()
        today_points = self._load_today_congestion_points()
        self.congestion.state.frame_time_stamps = [ts for ts, _ in today_points]
        self.congestion.state.frame_motion_scores = [v for _, v in today_points]
        self.congestion.state.smoothed_motion_scores = []
        self.congestion.smoother = CongestionSmoother(int(self.system_cfg.get("congestion_smoothing_window", 6)))
        for _, score in today_points:
            self.congestion.smoother.add(float(score))
            self.congestion.state.smoothed_motion_scores.append(self.congestion.smoother.current())
        self.congestion.state.current_congestion_index = today_points[-1][1] if today_points else 0.0
        self.congestion.state.current_smoothed_index = self.congestion.state.smoothed_motion_scores[-1] if today_points else 0.0

    def _get_display_id(self, track_id: int) -> int:
        if track_id not in self.display_id_map:
            self.display_id_map[track_id] = self.display_id_counter
            self.display_id_counter += 1
        return self.display_id_map[track_id]

    @staticmethod
    def _bbox_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        inter_x1, inter_y1 = max(ax1, bx1), max(ay1, by1)
        inter_x2, inter_y2 = min(ax2, bx2), min(ay2, by2)
        iw = max(0, inter_x2 - inter_x1)
        ih = max(0, inter_y2 - inter_y1)
        inter = iw * ih
        area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
        area_b = max(1, (bx2 - bx1) * (by2 - by1))
        union = max(1, area_a + area_b - inter)
        return inter / union

    def _deduplicate_overlapping_detections(self, raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        priority = {"truck": 0, "bus": 1, "car": 2, "motorcycle": 3, "bicycle": 4, "person": 5}
        used = [False] * len(raw_items)
        kept: list[dict[str, Any]] = []
        conf_eps = 0.05
        for i, item in enumerate(raw_items):
            if used[i]:
                continue
            cluster = [item]
            used[i] = True
            x1, y1, x2, y2 = item["bbox"]
            base_w = max(1.0, float(x2 - x1))
            base_h = max(1.0, float(y2 - y1))
            for j in range(i + 1, len(raw_items)):
                if used[j]:
                    continue
                cand = raw_items[j]
                iou = self._bbox_iou(item["bbox"], cand["bbox"])
                cx1, cy1 = item["center"]
                cx2, cy2 = cand["center"]
                dist = ((cx1 - cx2) ** 2 + (cy1 - cy2) ** 2) ** 0.5
                if iou >= 0.65 or dist <= min(base_w, base_h) * 0.35:
                    cluster.append(cand)
                    used[j] = True
            cluster.sort(key=lambda x: (-float(x.get("conf", 0.0)), priority.get(str(x.get("cls_name", "")).lower(), 999)))
            best = cluster[0]
            tid = int(best.get("track_id", -1))
            if tid >= 0:
                mem = self.track_class_memory.get(tid)
                if mem is not None:
                    prev_cls = str(mem.get("cls_name", best["cls_name"]))
                    prev_ts = mem.get("ts", datetime.min)
                    prev_area = float(mem.get("area", 1.0))
                    new_area = max(1.0, (best["bbox"][2] - best["bbox"][0]) * (best["bbox"][3] - best["bbox"][1]))
                    if (datetime.now() - prev_ts).total_seconds() <= 3.0 and abs(float(best.get("conf", 0)) - float(mem.get("conf", 0))) <= conf_eps:
                        if prev_cls != best["cls_name"] and 0.55 <= (new_area / max(1.0, prev_area)) <= 1.8:
                            best["cls_name"] = prev_cls
                self.track_class_memory[tid] = {"cls_name": best["cls_name"], "ts": datetime.now(), "area": max(1.0, (best["bbox"][2] - best["bbox"][0]) * (best["bbox"][3] - best["bbox"][1])), "conf": float(best.get("conf", 0.0))}
            kept.append(best)
        return kept

    @QtCore.pyqtSlot()
    def run(self) -> None:
        self._set_status("RUNNING")
        while self._running:
            try:
                payload = self.process_once_nonblocking()
                if payload is not None:
                    self.frame_ready.emit(payload)
            except Exception as exc:
                self.error_occurred.emit(self.camera_id, str(exc))
            QtCore.QThread.msleep(5)
        self._release_capture()
        self.finished.emit(self.camera_id)

    @QtCore.pyqtSlot()
    def stop(self) -> None:
        self._running = False
        self._release_capture()

    def process_once_nonblocking(self) -> dict[str, Any] | None:
        try:
            start = time.time()
            now_ts = time.time()
            if now_ts < self._next_reconnect_time or now_ts < self._next_infer_retry_time:
                return None

            try:
                if self.cap is None or not self.cap.isOpened():
                    self._reconnect_capture("cap not opened")
                    return None

                ok, frame = self.cap.read()
                if not ok or frame is None:
                    self.read_fail_count += 1
                    if self.read_fail_count >= self.max_read_fail_before_reconnect:
                        self.error_occurred.emit(self.camera_id, f"[WARN] cam{self.camera_id} frame read failed repeatedly. reconnecting.")
                        self._reconnect_capture("read failed")
                    return None
                self.read_fail_count = 0
            except cv2.error as exc:
                self.error_occurred.emit(self.camera_id, f"[WARN] cam{self.camera_id} OpenCV error: {exc}")
                self._reconnect_capture("cv2.error")
                return None
            except Exception as exc:
                self.error_occurred.emit(self.camera_id, f"[WARN] cam{self.camera_id} capture exception: {exc}")
                self._reconnect_capture("generic exception")
                return None

            self._set_status("RUNNING")
            src_h, src_w = frame.shape[:2]
            self.last_raw_frame = self._resize_for_display(frame.copy())
            now = datetime.now()
            self._rollover_if_needed(now)

            self.frame_index += 1
            frame_skip = max(1, int(self.camera_cfg.get("frame_skip", 1)))
            if self.frame_index % frame_skip != 0:
                return None

            try:
                result = self.model.track(
                    source=frame,
                    persist=True,
                    tracker="bytetrack.yaml",
                    verbose=False,
                    device=self.device,
                    conf=float(self.camera_cfg.get("confidence_threshold", 0.25)),
                    iou=float(self.camera_cfg.get("iou_threshold", 0.5)),
                    imgsz=int(self.camera_cfg.get("imgsz", 640)),
                    classes=sorted(self.target_classes) if self.target_classes else None,
                )[0]
            except Exception as exc:
                self._next_infer_retry_time = time.time() + 3.0
                self._set_status("MODEL ERROR")
                self.error_occurred.emit(self.camera_id, f"[ERROR] cam{self.camera_id} tracker failed: {exc}")
                return None

            boxes = result.boxes
            tracks: list[dict[str, Any]] = []
            raw_items: list[dict[str, Any]] = []
            pass_events: list[dict[str, Any]] = []
            long_stay_events: list[dict[str, Any]] = []
            long_stay_list: list[tuple[int, float]] = []
            exclude_polygon = self.camera_cfg.get("exclude_polygon", [])
            stay_zone = self.camera_cfg.get("stay_zone_polygon", [])

            if boxes is not None and boxes.id is not None:
                cls_array = boxes.cls.cpu().numpy() if boxes.cls is not None else []
                conf_array = boxes.conf.cpu().numpy() if boxes.conf is not None else []
                for i, (box, tid) in enumerate(zip(boxes.xyxy.cpu().numpy(), boxes.id.cpu().numpy())):
                    cls_idx = int(cls_array[i]) if len(cls_array) > i else -1
                    if self.target_classes and cls_idx not in self.target_classes:
                        continue
                    cls_name = self.model.names.get(cls_idx, str(cls_idx)) if isinstance(self.model.names, dict) else str(cls_idx)
                    conf = float(conf_array[i]) if len(conf_array) > i else 0.0

                    x1, y1, x2, y2 = box.tolist()
                    bbox_h = max(1.0, y2 - y1)
                    judge_x = (x1 + x2) / 2
                    judge_y = y2 - (bbox_h * 0.2)
                    if self._in_polygon((judge_x, judge_y), exclude_polygon):
                        continue

                    track_id = int(tid)
                    display_id = self._get_display_id(track_id)
                    raw_items.append({
                        "track_id": track_id,
                        "display_id": display_id,
                        "cls_idx": cls_idx,
                        "cls_name": cls_name,
                        "conf": conf,
                        "bbox": (int(x1), int(y1), int(x2), int(y2)),
                        "center": (judge_x, judge_y),
                        "judge_point": (judge_x, judge_y),
                    })

                for item in self._deduplicate_overlapping_detections(raw_items):
                    track_id = int(item["track_id"])
                    display_id = int(item.get("display_id", track_id))
                    cls_name = item["cls_name"]
                    judge_x, judge_y = item["center"]
                    tracks.append(
                        {
                            "track_id": track_id,
                            "display_id": display_id,
                            "center": (judge_x, judge_y),
                            "judge_point": (judge_x, judge_y),
                            "bbox": item["bbox"],
                            "class_name": cls_name,
                            "crossed": False,
                        }
                    )

                    event = self.counter.update(track_id, (judge_x, judge_y), cls_name, now)
                    if event:
                        pass_events.append(event)
                        self.wakimura_alpha.record_exit(now)
                        self.cross_flash_frames[track_id] = 12
                        tracks[-1]["crossed"] = True

                    if track_id not in self.track_state.first_seen:
                        self.track_state.first_seen[track_id] = now

                    in_stay_zone = self._in_polygon((judge_x, judge_y), stay_zone) if stay_zone else True
                    if in_stay_zone:
                        stay_mins = (now - self.track_state.first_seen[track_id]).total_seconds() / 60.0
                        if stay_mins >= float(self.camera_cfg.get("long_stay_minutes", 15)):
                            long_stay_list.append((display_id, stay_mins))
                            if track_id not in self.track_state.long_stay_emitted:
                                self.track_state.long_stay_emitted.add(track_id)
                                long_stay_events.append({
                                    "first_seen": self.track_state.first_seen[track_id].strftime("%Y-%m-%d %H:%M:%S"),
                                    "detected_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                                    "camera_id": self.camera_id,
                                    "track_id": track_id,
                                    "stay_minutes": round(stay_mins, 1),
                                    "class_name": cls_name,
                                })
                    elif self.cross_flash_frames.get(track_id, 0) > 0:
                        tracks[-1]["crossed"] = True

            for tid in list(self.cross_flash_frames):
                self.cross_flash_frames[tid] -= 1
                if self.cross_flash_frames[tid] <= 0:
                    self.cross_flash_frames.pop(tid, None)

            frame_width = int(frame.shape[1]) if frame is not None else 1920
            prev_points_len = len(self.congestion.state.frame_time_stamps)
            congestion_score = self.congestion.update(tracks, now, frame_width) if self.camera_cfg.get("enable_congestion", True) else 0.0
            threshold = float(self.camera_cfg.get("congestion_threshold", 5))
            threshold_over = congestion_score >= threshold
            count_ltor = int(sum(self.counter.state.pass_bins_ltor))
            count_rtol = int(sum(self.counter.state.pass_bins_rtol))

            # tracks 上の現在IDに対して滞在秒数平均を別計算（脇村指標 α 用）
            avg_stay_sec = 0.0
            if tracks:
                stay_secs = []
                for tr in tracks:
                    seen = self.track_state.first_seen.get(int(tr["track_id"]))
                    if seen is not None:
                        stay_secs.append(max(0.0, (now - seen).total_seconds()))
                avg_stay_sec = float(np.mean(stay_secs)) if stay_secs else 0.0
            movement_values: list[float] = []
            for tr in tracks:
                tid = int(tr["track_id"])
                cx, cy = tr["center"]
                prev = self.congestion.state.previous_positions.get(tid)
                if prev is None:
                    continue
                px, py = prev
                movement_values.append(float(((cx - px) ** 2 + (cy - py) ** 2) ** 0.5))
            stop_count = sum(1 for d in movement_values if d <= 1.0)
            slow_count = sum(1 for d in movement_values if d <= 3.0)
            below_threshold_count = sum(1 for d in movement_values if d <= 2.0)
            track_count = len(tracks)
            max_stay_min = max((mins for _, mins in long_stay_list), default=0.0)
            move_avg = float(np.mean(movement_values)) if movement_values else 0.0
            move_median = float(np.median(movement_values)) if movement_values else 0.0
            score_sum = float(self.congestion.state.frame_cumulative_motion_score)
            score_avg = float(congestion_score)

            wakimura = {
                "wakimura_alpha": 0.0,
                "wakimura_alpha_window": 0.0,
                "wakimura_n_out": 0,
                "wakimura_avg_stay_sec": 0.0,
                "wakimura_high_load_mode": False,
            }
            try:
                wakimura = self.wakimura_alpha.update(now=now, vehicle_count=len(tracks), avg_stay_sec=avg_stay_sec)
            except Exception as exc:
                self.error_occurred.emit(self.camera_id, f"[WARN] cam{self.camera_id} 脇村指標 α update skipped: {exc}")

            elapsed = max(1e-6, time.time() - start)
            self.fps = 1.0 / elapsed

            try:
                for pe in pass_events:
                    self._append_csv_safe(self.pass_csv, [pe["timestamp"], self.camera_id, pe["track_id"], pe["class_name"], pe["direction"]], kind="pass")
                for le in long_stay_events:
                    self._append_csv_safe(
                        self.long_stay_csv,
                        [le["first_seen"], le["detected_at"], le["camera_id"], le["track_id"], le["stay_minutes"], le["class_name"]],
                        kind="long_stay",
                    )
            except Exception as exc:
                self.error_occurred.emit(self.camera_id, f"[WARN] cam{self.camera_id} csv helper skipped: {exc}")

            if len(self.congestion.state.frame_time_stamps) > prev_points_len:
                try:
                    self._append_csv_safe(
                        self.congestion_csv,
                        [
                            now.strftime("%Y-%m-%d %H:%M:%S"),
                            self.camera_id,
                            self.camera_name,
                            round(congestion_score, 2),
                            round(threshold, 2),
                            bool(threshold_over),
                            round(self.fps, 2),
                        ],
                        kind="congestion",
                    )
                except Exception as exc:
                    self.error_occurred.emit(self.camera_id, f"[WARN] cam{self.camera_id} congestion csv skipped: {exc}")

            self.last_frame = self._resize_for_display(self._draw_overlay(frame.copy(), tracks))
            try:
                long_stay_list.sort(key=lambda x: x[1], reverse=True)
            except Exception as exc:
                self.error_occurred.emit(self.camera_id, f"[WARN] cam{self.camera_id} long_stay list sort skipped: {exc}")
                long_stay_list = []
            smoothed_score = float(self.congestion.state.current_smoothed_index)

            return {
                "camera_id": self.camera_id,
                "camera_name": self.camera_name,
                "stream_name": self.camera_cfg.get("stream_name", self.camera_cfg.get("stream_url", "")),
                "frame": self.last_frame,
                "source_frame_width": int(src_w),
                "source_frame_height": int(src_h),
                "congestion_score": congestion_score,
                "smoothed_congestion_score": smoothed_score,
                "congestion_level": 1,
                "threshold": threshold,
                "threshold_over": threshold_over,
                "congestion_points": list(zip(self.congestion.state.frame_time_stamps, self.congestion.state.frame_motion_scores)),
                "prev_congestion_points": self.previous_day_congestion_points,
                "pass_bins_ltor": self.counter.state.pass_bins_ltor,
                "pass_bins_rtol": self.counter.state.pass_bins_rtol,
                "count_ltor": count_ltor,
                "count_rtol": count_rtol,
                "hist_prev_ltor": self.previous_day_hist_ltor,
                "hist_prev_rtol": self.previous_day_hist_rtol,
                "long_stay_count": len(long_stay_list),
                "long_stay_list": [[int(tid), float(minutes)] for tid, minutes in long_stay_list[:10]],
                **wakimura,
                "wak_stop_count": int(stop_count),
                "wak_slow_count": int(slow_count),
                "wak_total_tracks": int(track_count),
                "wak_avg_move": float(move_avg),
                "wak_median_move": float(move_median),
                "wak_below_threshold_count": int(below_threshold_count),
                "wak_below_threshold_ratio": float((below_threshold_count / track_count) if track_count else 0.0),
                "wak_stop_ratio": float((stop_count / track_count) if track_count else 0.0),
                "wak_slow_ratio": float((slow_count / track_count) if track_count else 0.0),
                "wak_window_count": int(len(self.congestion.state.frame_time_stamps)),
                "wak_score_sum": float(score_sum),
                "wak_score_avg": float(score_avg),
                "wak_max_stay_min": float(max_stay_min),
                "wak_stay_over_1min": int(sum(1 for _, mins in long_stay_list if mins >= 1.0)),
                "wak_updated_at": now.strftime("%H:%M:%S"),
                "debug_metrics": {
                    "raw_congestion_index": round(float(congestion_score), 3),
                    "smoothed_congestion_index": round(smoothed_score, 3),
                    "level": 1,
                    "last_update_timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                },
                "fps": self.fps,
                "status": self._status_text,
                "device": self.device,
                "gpu_name": self.gpu_name,
            }
        except Exception as exc:
            self.error_occurred.emit(self.camera_id, f"[WARN] cam{self.camera_id} loop exception: {exc}")
            self._reconnect_capture("loop exception")
            return None

    def _resize_for_display(self, frame: np.ndarray) -> np.ndarray:
        if frame is None:
            return frame
        if self.display_scale >= 0.999:
            return frame
        h, w = frame.shape[:2]
        new_w = max(1, int(w * self.display_scale))
        new_h = max(1, int(h * self.display_scale))
        return cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

    def _draw_overlay(self, frame: np.ndarray, tracks: list[dict[str, Any]]) -> np.ndarray:
        line = [self.camera_cfg.get("line_start", [10, 10]), self.camera_cfg.get("line_end", [100, 10])]
        cv2.line(frame, tuple(line[0]), tuple(line[1]), (0, 255, 255), 2)

        poly = self.camera_cfg.get("exclude_polygon", [])
        if len(poly) >= 3:
            cv2.polylines(frame, [np.array(poly, np.int32)], isClosed=True, color=(255, 255, 0), thickness=2)
            cv2.putText(frame, "EXCLUDE", tuple(np.array(poly[0], dtype=int)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 2, cv2.LINE_AA)

        for tr in tracks:
            x1, y1, x2, y2 = tr["bbox"]
            box_color = (0, 0, 255) if tr.get("crossed", False) or self.cross_flash_frames.get(int(tr["track_id"]), 0) > 0 else (0, 255, 0)
            cv2.rectangle(frame, (x1, y1), (x2, y2), box_color, 2)
            px, py = tr.get("judge_point", tr["center"])
            cv2.circle(frame, (int(px), int(py)), 3, (255, 255, 255), -1)
            cv2.putText(
                frame,
                f"ID:{int(tr.get('display_id', tr['track_id'])):03d} {tr['class_name']}",
                (x1, max(20, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                box_color,
                2,
                cv2.LINE_AA,
            )

        return frame

# =========================================
# Main Window
# =========================================
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, root_dir: Path):
        super().__init__()
        self.root_dir = root_dir
        self.setWindowTitle("AI Congestion Monitor")
        self.setStyleSheet("background:#02060a;")
        self.resize(1080, 1600)
        self.setMinimumSize(1080, 1300)

        self.cfg_mgr = ConfigManager(root_dir)
        self.app_cfg = self.cfg_mgr.load()
        self.reporter = ReportWriter(root_dir / "data")
        self.ten_min_writer = TenMinuteRecordWriter(root_dir / "data")
        raw_ai_status = Path(self.app_cfg.system.get("ai_status_json_path", "app/11_config/ai_status.json"))
        script_base = Path(__file__).resolve().parents[2]
        if raw_ai_status.is_absolute():
            ai_status_path = raw_ai_status
        else:
            ai_status_path = script_base / raw_ai_status
        ai_status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_mgr = StatusManager(ai_status_path, self.app_cfg.system)

        self.threads: dict[int, QtCore.QThread] = {}
        self.workers: dict[int, CameraWorker] = {}
        self.panels: dict[int, CameraPanel] = {}
        self.latest_payloads: dict[int, dict[str, Any]] = {}
        self.ten_min_buffer: list[dict[str, Any]] = []
        now_dt = datetime.now()
        self.current_10min_bin_start = self.ten_min_writer._floor_to_10min(now_dt)
        self.current_10min_date = now_dt.date()
        self.system_level_history_today: list[tuple[datetime, float]] = []
        self.system_level_history_yesterday: list[tuple[datetime, float]] = []
        self.system_level_history_date = datetime.now().date()
        self.last_system_level_record_ts = 0.0

        toolbar = QtWidgets.QToolBar("Main", self)
        toolbar.setMovable(False)
        self.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, toolbar)
        action_setting = toolbar.addAction("解析条件")
        action_setting.triggered.connect(self.open_settings)
        action_save = toolbar.addAction("保存")
        action_save.triggered.connect(self.save_current_settings)
        action_load = toolbar.addAction("読込")
        action_load.triggered.connect(self.load_settings_from_json)
        action_daily = toolbar.addAction("日次Excel出力")
        action_daily.triggered.connect(self.export_daily)
        action_monthly = toolbar.addAction("月次Excel出力")
        action_monthly.triggered.connect(self.export_monthly)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setCentralWidget(scroll)
        content = QtWidgets.QWidget()
        content.setMinimumWidth(1080)
        content.setMaximumWidth(1080)
        content.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Preferred)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        scroll.setWidget(content)

        top_info_grid = QtWidgets.QGridLayout()
        top_info_grid.setContentsMargins(0, 0, 0, 0)
        top_info_grid.setHorizontalSpacing(6)
        top_info_grid.setVerticalSpacing(4)
        self.congestion_formula_label = QtWidgets.QLabel(
            "渋滞指数＝車両の停滞傾向を表す指標。移動量が小さい車ほど値が高くなり、流れが悪い状態を表す。\n"
            "直近30秒の各車両の移動量 d を用いて score=Σ[1/(1+d/(W×500))] を算出し、画面全体の停滞感を集計する。"
        )
        self.congestion_formula_label.setWordWrap(True)
        self.congestion_formula_label.setStyleSheet(
            "color:#9af2ff;background:#08121b;border:1px solid #1f4f7a;padding:4px;font-size:13px;"
        )
        self.wakimura_formula_label = QtWidgets.QLabel(
            "脇村指標は、停止寄り車両の割合や滞在傾向を補助的に見るための参考指標。\n"
            "停止台数・低速台数・平均移動量・score情報を用いて、渋滞指数だけでは見えにくい詰まり方を補足する。"
        )
        self.wakimura_formula_label.setWordWrap(True)
        self.wakimura_formula_label.setStyleSheet(
            "color:#d6cbff;background:#0d1020;border:1px solid #4b3cb0;padding:4px;font-size:13px;"
        )
        self.congestion_formula_label.setFixedWidth(640)
        self.wakimura_formula_label.setFixedWidth(640)

        top_left_box = QtWidgets.QVBoxLayout()
        top_left_box.setSpacing(2)
        top_left_box.setContentsMargins(4, 4, 4, 4)
        top_left_widget = QtWidgets.QWidget()
        top_left_widget.setLayout(top_left_box)
        top_left_widget.setFixedWidth(640)

        formula_stack = QtWidgets.QVBoxLayout()
        formula_stack.setContentsMargins(0, 0, 0, 0)
        formula_stack.setSpacing(3)
        formula_widget = QtWidgets.QWidget()
        formula_widget.setLayout(formula_stack)
        formula_stack.addWidget(self.congestion_formula_label)
        formula_stack.addWidget(self.wakimura_formula_label)

        level_block = QtWidgets.QVBoxLayout()
        level_block.setContentsMargins(0, 0, 0, 0)
        level_block.setSpacing(3)
        level_block_widget = QtWidgets.QWidget()
        level_block_widget.setLayout(level_block)
        level_block_widget.setFixedWidth(420)
        self.level_badge = QtWidgets.QLabel("🟢 渋滞LEVEL1")
        self.level_badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.level_badge.setMinimumHeight(58)
        self.level_badge.setStyleSheet("background:#7fd0ff;color:#000000;border-radius:8px;font-weight:900;font-size:36px;padding:4px 10px;")
        self.system_title_ja = QtWidgets.QLabel("AI渋滞判定システム")
        self.system_title_ja.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.system_title_ja.setStyleSheet("font-size:26px;font-weight:900;color:#9fe8ff;")
        self.system_title_en = QtWidgets.QLabel("AI Congestion Detection System")
        self.system_title_en.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.system_title_en.setStyleSheet("font-size:14px;font-weight:bold;color:#b7dbff;")
        self.system_runtime_label = QtWidgets.QLabel("GPU: n/a ｜ model: n/a ｜ tracker: ByteTrack")
        self.system_runtime_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.system_runtime_label.setStyleSheet("font-size:11px;color:#9abed0;")
        top_left_box.addWidget(self.system_title_ja)
        top_left_box.addWidget(self.system_title_en)
        top_left_box.addWidget(self.system_runtime_label)
        self.level_rule_label = QtWidgets.QLabel(
            "<b>LEVEL1：</b>通常時<br>"
            "<b>LEVEL2：</b>[KING]渋滞指数5以上 + 5分以上滞在台数3台以上<br>"
            "<b>LEVEL3：</b>[QUEEN]渋滞指数3以上<br>"
            "<b>LEVEL4：</b>[QUEEN]渋滞指数3以上 + [JACK]渋滞指数3以上"
        )
        self.level_rule_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self.level_rule_label.setWordWrap(True)
        self.level_rule_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.level_rule_label.setStyleSheet("color:#b7dbff;background:#0a1420;border:1px solid #1f4f7a;padding:4px;font-size:12px;line-height:1.25em;")
        level_block.addWidget(self.level_badge)

        top_info_grid.addWidget(top_left_widget, 0, 0)
        top_info_grid.addWidget(level_block_widget, 0, 1)
        top_info_grid.addWidget(formula_widget, 1, 0)
        top_info_grid.addWidget(self.level_rule_label, 1, 1)
        layout.addLayout(top_info_grid)

        self.system_level_graph = CombinedTimelineGraph("line")
        self.system_level_graph.setFixedHeight(60)
        self.system_level_graph.set_y_axis_config(
            y_min=1.0,
            y_max=4.0,
            labels={1.0: "LEVEL1", 2.0: "LEVEL2", 3.0: "LEVEL3", 4.0: "LEVEL4"},
        )
        layout.addWidget(self.system_level_graph)
        self.system_level_history_yesterday = self._load_system_level_history(self.system_level_history_date - timedelta(days=1))
        self.system_level_history_today = self._load_system_level_history(self.system_level_history_date)
        self.update_level_rule_text()
        self._refresh_system_level_graph()

        for cam in self.app_cfg.cameras:
            if not cam.get("enabled", True):
                continue
            panel = CameraPanel(cam)
            panel.line_setting_requested.connect(lambda cid, m="line": self.open_settings_for_camera(cid, m))
            panel.exclude_setting_requested.connect(lambda cid, m="poly": self.open_settings_for_camera(cid, m))
            panel.camera_setting_requested.connect(lambda cid, m="basic": self.open_settings_for_camera(cid, m))
            panel.threshold_changed.connect(self.on_threshold_changed)
            layout.addWidget(panel, 1)
            self.panels[cam["camera_id"]] = panel
            try:
                worker = CameraWorker(cam, self.app_cfg.system, self.root_dir)
            except Exception as exc:
                panel.set_status("MODEL ERROR")
                logging.exception("cam%s worker init failed: %s", cam.get("camera_id"), exc)
                continue
            thread = QtCore.QThread(self)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.frame_ready.connect(self.on_camera_payload)
            worker.error_occurred.connect(self.on_camera_error)
            worker.status_changed.connect(self.on_camera_status_changed)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)

            cid = cam["camera_id"]
            self.workers[cid] = worker
            self.threads[cid] = thread
            thread.start()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(self.app_cfg.system.get("display_update_interval_ms", 800)))
        QtCore.QTimer.singleShot(0, self._show_on_target_screen)

    def tick(self) -> None:
        now = datetime.now()
        self._rollover_system_level_history_if_needed(now)
        self._update_status_level()
        self._update_global_status()
        interval_sec = max(1, int(self.app_cfg.system.get("graph_update_interval_sec", 10)))
        now_ts = time.time()
        if now_ts - self.last_system_level_record_ts >= interval_sec:
            self._record_system_level(self.compute_system_level(), now)
            self.last_system_level_record_ts = now_ts
            self._refresh_system_level_graph()
        self._collect_and_flush_10min_records(now)

    @QtCore.pyqtSlot(dict)
    def on_camera_payload(self, payload: dict[str, Any]) -> None:
        cid = int(payload.get("camera_id", -1))
        if cid in self.panels:
            try:
                self.panels[cid].update_view(payload)
            except Exception as exc:
                logging.exception("[ERROR] cam%s update_view failed: %s", cid, exc)
        self.latest_payloads[cid] = payload
        try:
            self._update_status_level()
        except Exception as exc:
            logging.warning("on_camera_payload status update skipped cam%s: %s", cid, exc)

    def _show_on_target_screen(self) -> None:
        try:
            screens = QtGui.QGuiApplication.screens()
            if not screens:
                self.showMaximized()
                return
            target_index = 2 if len(screens) >= 3 else len(screens) - 1
            target_screen = screens[target_index]
            geom = target_screen.availableGeometry()
            self.setGeometry(geom)
            self.move(geom.topLeft())
        except Exception as exc:
            logging.warning("show_on_target_screen fallback to maximized: %s", exc)
        self.showMaximized()

    @QtCore.pyqtSlot(int, str)
    def on_camera_error(self, camera_id: int, message: str) -> None:
        logging.warning("camera %s: %s", camera_id, message)

    @QtCore.pyqtSlot(int, str)
    def on_camera_status_changed(self, camera_id: int, status_text: str) -> None:
        panel = self.panels.get(camera_id)
        if panel is not None:
            panel.set_status(status_text)

    def _update_status_level(self) -> None:
        overall_level = self.compute_system_level()
        try:
            self.status_mgr.update_if_needed(int(overall_level))
        except Exception as exc:
            logging.warning("ai_status update skipped: %s", exc)

    def _find_payload_by_camera_name(self, camera_name: str) -> dict[str, Any] | None:
        for payload in self.latest_payloads.values():
            if str(payload.get("camera_name", "")).strip() == camera_name:
                return payload
        return None

    def get_camera_congestion_score(self, camera_name: str) -> float:
        payload = self._find_payload_by_camera_name(camera_name)
        if payload is None:
            return 0.0
        return float(payload.get("congestion_score", 0.0))

    def get_camera_long_stay_count(self, camera_name: str, minutes: int = 5) -> int:
        payload = self._find_payload_by_camera_name(camera_name)
        if payload is None:
            return 0
        long_stay_list = payload.get("long_stay_list", [])
        count = 0
        for entry in long_stay_list:
            try:
                if float(entry[1]) >= float(minutes):
                    count += 1
            except (TypeError, ValueError, IndexError):
                continue
        return count

    def get_camera_threshold(self, camera_name: str) -> float:
        for cam in self.app_cfg.cameras:
            if str(cam.get("camera_name", "")).strip() == camera_name:
                return float(cam.get("congestion_threshold", 5.0))
        return 5.0

    def compute_system_level(self) -> int:
        cam1_score = self.get_camera_congestion_score("Camera1")
        cam2_score = self.get_camera_congestion_score("Camera2")
        cam3_score = self.get_camera_congestion_score("Camera3")
        cam1_th = self.get_camera_threshold("Camera1")
        cam2_th = self.get_camera_threshold("Camera2")
        cam3_th = self.get_camera_threshold("Camera3")
        cam2_long_stay_5min = self.get_camera_long_stay_count("Camera2", minutes=5)
        if cam1_score >= cam1_th and cam3_score >= cam3_th:
            return 4
        if cam1_score >= cam1_th:
            return 3
        if cam2_score >= cam2_th and cam2_long_stay_5min >= 3:
            return 2
        return 1

    def update_level_rule_text(self) -> None:
        cam1_th = self.get_camera_threshold("Camera1")
        cam2_th = self.get_camera_threshold("Camera2")
        cam3_th = self.get_camera_threshold("Camera3")
        self.level_rule_label.setText(
            "<b>LEVEL1：</b>通常時<br>"
            f"<b>LEVEL2：</b>[KING]渋滞指数{cam2_th:.1f}以上 + 5分以上滞在台数3台以上<br>"
            f"<b>LEVEL3：</b>[QUEEN]渋滞指数{cam1_th:.1f}以上<br>"
            f"<b>LEVEL4：</b>[QUEEN]渋滞指数{cam1_th:.1f}以上 + [JACK]渋滞指数{cam3_th:.1f}以上"
        )

    @QtCore.pyqtSlot(int, float)
    def on_threshold_changed(self, camera_id: int, value: float) -> None:
        try:
            target = next((cam for cam in self.app_cfg.cameras if int(cam.get("camera_id", -1)) == int(camera_id)), None)
            if target is None:
                return
            target["congestion_threshold"] = float(value)
            self.cfg_mgr.save_camera_settings(self.app_cfg.cameras)
            worker = self.workers.get(camera_id)
            if worker is not None:
                worker.update_camera_config({"congestion_threshold": float(value)})
            self.update_level_rule_text()
            self._update_status_level()
            self._update_global_status()
        except Exception as exc:
            logging.warning("cam%s threshold update failed: %s", camera_id, exc)

    def _update_global_status(self) -> None:
        model_name = (
            self.app_cfg.system.get("yolo_model")
            or self.app_cfg.system.get("YOLO_MODEL")
            or self.app_cfg.system.get("model_path")
            or "n/a"
        )
        gpu = next(iter(self.latest_payloads.values()), {}).get("gpu_name", "n/a")
        self.system_runtime_label.setText(
            f"GPU: {gpu} ｜ model: {model_name} ｜ tracker: ByteTrack"
        )
        level = self.compute_system_level()
        style = level_style(level)
        self.level_badge.setText(f"{style['icon']} 渋滞LEVEL{level}")
        self.level_badge.setStyleSheet(
            f"background:{style['bg']};color:{style['fg']};border-radius:8px;font-weight:900;font-size:36px;padding:4px 14px;"
        )

    def _system_level_metrics_dir(self) -> Path:
        metrics_dir = self.root_dir / "data" / "metrics" / "system"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        return metrics_dir

    def _system_level_history_csv_path(self, target_date: date) -> Path:
        return self._system_level_metrics_dir() / f"system_level_history_{target_date.isoformat()}.csv"

    def _ensure_system_level_history_csv(self, target_date: date) -> Path:
        csv_path = self._system_level_history_csv_path(target_date)
        if csv_path.exists():
            return csv_path
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "system_level"])
        return csv_path

    def _load_system_level_history(self, target_date: date) -> list[tuple[datetime, float]]:
        csv_path = self._system_level_history_csv_path(target_date)
        points: list[tuple[datetime, float]] = []
        if not csv_path.exists():
            return points
        with csv_path.open("r", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    ts = datetime.strptime(str(row.get("timestamp", "")), "%Y-%m-%d %H:%M:%S")
                    level = float(row.get("system_level", "0"))
                    if 1.0 <= level <= 4.0:
                        points.append((ts, level))
                except Exception:
                    continue
        return points

    def _rollover_system_level_history_if_needed(self, now: datetime) -> None:
        if now.date() == self.system_level_history_date:
            return
        self.system_level_history_date = now.date()
        self.system_level_history_yesterday = self._load_system_level_history(now.date() - timedelta(days=1))
        self.system_level_history_today = self._load_system_level_history(now.date())

    def _record_system_level(self, level: int, now: datetime) -> None:
        level_val = float(max(1, min(4, int(level))))
        if self.system_level_history_today and (now - self.system_level_history_today[-1][0]).total_seconds() < 1.0:
            self.system_level_history_today[-1] = (now, level_val)
            return
        self.system_level_history_today.append((now, level_val))
        csv_path = self._ensure_system_level_history_csv(now.date())
        with csv_path.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([now.strftime("%Y-%m-%d %H:%M:%S"), int(level_val)])

    def _refresh_system_level_graph(self) -> None:
        self.system_level_graph.set_line_data(
            self.system_level_history_yesterday,
            self.system_level_history_today,
            "全体渋滞レベル履歴（0-24時）",
            threshold=None,
            show_threshold=False,
        )

    def open_settings(self) -> None:
        cams = self.app_cfg.cameras
        names = [f"{c['camera_id']}: {c['camera_name']}" for c in cams]
        selected, ok = QtWidgets.QInputDialog.getItem(self, "対象カメラ", "設定するカメラ", names, 0, False)
        if not ok:
            return
        camera_id = int(selected.split(":", 1)[0])
        self.open_settings_for_camera(camera_id, "basic")

    def open_settings_for_camera(self, camera_id: int, mode: str = "basic") -> None:
        cams = self.app_cfg.cameras
        idx = next(i for i, c in enumerate(cams) if c["camera_id"] == camera_id)
        live = self.workers[camera_id].get_latest_raw_frame() if camera_id in self.workers else None
        initial_mode = "line" if mode in {"line", "basic"} else "poly"
        dlg = CameraSettingsDialog(cams[idx], live, self, initial_mode=initial_mode)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        cams[idx] = dlg.get_updated_config()
        self.cfg_mgr.save_camera_settings(cams)
        try:
            self.workers[camera_id].update_camera_config(cams[idx])
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "モデル更新失敗", str(exc))
            return
        QtWidgets.QMessageBox.information(self, "保存", "設定を保存し、次フレームから反映しました。")

    def save_current_settings(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "設定保存", str(self.root_dir / "config" / "monitor_config.json"), "JSON (*.json)")
        if not path:
            return
        data = {"system": self.app_cfg.system, "cameras": self.app_cfg.cameras}
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        QtWidgets.QMessageBox.information(self, "保存", f"保存しました: {path}")

    def load_settings_from_json(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "設定読込", str(self.root_dir / "config"), "JSON (*.json)")
        if not path:
            return
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.app_cfg.system.update(data.get("system", {}))
        self.app_cfg.cameras = data.get("cameras", self.app_cfg.cameras)
        self.cfg_mgr.save_system_settings(self.app_cfg.system)
        self.cfg_mgr.save_camera_settings(self.app_cfg.cameras)
        for cam in self.app_cfg.cameras:
            cid = cam["camera_id"]
            if cid in self.workers:
                try:
                    self.workers[cid].update_camera_config(cam)
                except Exception as exc:
                    QtWidgets.QMessageBox.critical(self, "設定反映失敗", str(exc))
        QtWidgets.QMessageBox.information(self, "読込", "設定を反映しました。")

    def export_daily(self) -> None:
        path = self.reporter.write_daily_report(date.today(), self.app_cfg.cameras, self.root_dir / "data" / "metrics")
        self._export_multi_day_plot()
        QtWidgets.QMessageBox.information(self, "日次", f"出力完了: {path}")

    def export_monthly(self) -> None:
        month = datetime.now().strftime("%Y-%m")
        path = self.reporter.write_monthly_report(month, self.root_dir / "data" / "metrics", self.app_cfg.cameras)
        QtWidgets.QMessageBox.information(self, "月次", f"出力完了: {path}")

    def _export_multi_day_plot(self) -> None:
        metrics_files = []
        for cam in self.app_cfg.cameras:
            cid = cam["camera_id"]
            cam_dir = self.root_dir / "data" / "metrics" / f"cam{cid}"
            metrics_files.extend(sorted(cam_dir.glob("congestion_timeseries_*.csv"))[-7:])
        out = self.root_dir / "data" / "reports" / "daily" / f"multi_day_trend_{date.today().isoformat()}.png"
        save_multi_day_trend_plot(metrics_files, "congestion_score", out)

    def _collect_and_flush_10min_records(self, now: datetime) -> None:
        level = self.compute_system_level()
        self.ten_min_buffer.append(self.ten_min_writer.collect_sample(now, level, self.latest_payloads))
        active_bin = self.ten_min_writer._floor_to_10min(now)
        if active_bin == self.current_10min_bin_start:
            return
        self._flush_current_10min_bin()
        prev_date = self.current_10min_date
        self.current_10min_bin_start = active_bin
        self.current_10min_date = active_bin.date()
        if self.current_10min_date != prev_date:
            self.ten_min_writer.finalize_day_graph(prev_date)

    def _flush_current_10min_bin(self) -> None:
        if not self.ten_min_buffer:
            return
        row = self.ten_min_writer.aggregate_10min(self.current_10min_bin_start, self.ten_min_buffer)
        self.ten_min_writer.append_row(self.current_10min_bin_start.date(), row)
        self.ten_min_buffer.clear()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._flush_current_10min_bin()
        for worker in self.workers.values():
            try:
                worker.stop()
            except Exception:
                pass

        for thread in self.threads.values():
            thread.quit()
            thread.wait(3000)

        super().closeEvent(event)


MonitorMainWindow = MainWindow


# =========================================
# parse_args / main
# =========================================
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="3-camera AI congestion monitor")
    p.add_argument("--root", default=str(Path(__file__).resolve().parent), help="04_ai_monitor root directory")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    root_dir = Path(args.root)
    setup_logging(root_dir / "logs")
    logging.info("AI monitor starting. root=%s", root_dir)
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow(root_dir)
    win.show()
    try:
        return app.exec()
    except Exception:
        err_path = root_dir / "logs" / "ai_monitor_last_traceback.log"
        err_path.write_text(traceback.format_exc(), encoding="utf-8")
        logging.exception("fatal exception in QApplication loop")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
