from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch
from ultralytics import YOLO

from .congestion_logic import CongestionScorer
from .counter_logic import LineCounter


@dataclass
class TrackState:
    first_seen: dict[int, datetime] = field(default_factory=dict)
    long_stay_emitted: set[int] = field(default_factory=set)


class CameraWorker:
    """カメラ単位で独立運用する処理クラス（将来拡張前提）。"""

    def __init__(
        self,
        camera_cfg: dict[str, Any],
        system_cfg: dict[str, Any],
        root_dir: Path,
    ):
        self.camera_cfg = camera_cfg
        self.system_cfg = system_cfg
        self.root_dir = root_dir
        self.camera_id = int(camera_cfg["camera_id"])
        self.camera_name = camera_cfg["camera_name"]

        self.device = self._resolve_device(system_cfg.get("device_preference", "auto"))
        self.gpu_name = torch.cuda.get_device_name(0) if self.device.startswith("cuda") else "CPU"

        self.model = YOLO(system_cfg.get("model_path", "yolo11n.pt"))
        self.target_classes = set(system_cfg.get("target_classes", ["car", "bus", "truck", "motorcycle"]))

        self.counter = LineCounter(camera_cfg.get("direction", "LtoR"), camera_cfg.get("line_points", [[0, 0], [100, 0]]))
        self.congestion = CongestionScorer()
        self.track_state = TrackState()

        self.cap = None
        self.last_frame = None
        self.fps = 0.0

        self.today = datetime.now().date()
        self.metrics_dir = root_dir / "data" / "metrics" / f"cam{self.camera_id}"
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        self.realtime_csv, self.pass_csv, self.long_stay_csv = self._ensure_daily_csvs()
        self.previous_day_hist = self._load_previous_day_histogram()

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
        self.cap = cv2.VideoCapture(self._stream_source())

    def _in_polygon(self, point: tuple[float, float], polygon_points: list[list[int]]) -> bool:
        if not polygon_points:
            return False
        poly = np.array(polygon_points, np.int32)
        return cv2.pointPolygonTest(poly, point, False) >= 0

    def _ensure_daily_csvs(self):
        date_str = self.today.strftime("%Y-%m-%d")
        realtime = self.metrics_dir / f"realtime_metrics_{date_str}.csv"
        pass_events = self.metrics_dir / f"pass_events_{date_str}.csv"
        long_stay = self.metrics_dir / f"long_stay_events_{date_str}.csv"

        self._ensure_csv_header(realtime, [
            "timestamp", "camera_id", "camera_name", "active_tracks", "congestion_score",
            "congestion_threshold", "threshold_over", "long_stay_count", "fps",
        ])
        self._ensure_csv_header(pass_events, [
            "timestamp", "camera_id", "track_id", "class_name", "direction",
        ])
        self._ensure_csv_header(long_stay, [
            "first_seen", "detected_at", "camera_id", "track_id", "stay_minutes", "class_name",
        ])
        return realtime, pass_events, long_stay

    @staticmethod
    def _ensure_csv_header(path: Path, header: list[str]) -> None:
        if path.exists():
            return
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(header)

    def _append_csv(self, path: Path, row: list[Any]) -> None:
        with path.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)

    def _load_previous_day_histogram(self) -> list[int]:
        prev = self.today.fromordinal(self.today.toordinal() - 1)
        prev_file = self.metrics_dir / f"pass_events_{prev.strftime('%Y-%m-%d')}.csv"
        hist = [0] * 144
        if not prev_file.exists():
            return hist
        with prev_file.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                dt = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                idx = (dt.hour * 60 + dt.minute) // 10
                if 0 <= idx < 144:
                    hist[idx] += 1
        return hist

    def _rollover_if_needed(self, now: datetime) -> None:
        if now.date() == self.today:
            return
        self.today = now.date()
        self.realtime_csv, self.pass_csv, self.long_stay_csv = self._ensure_daily_csvs()
        self.previous_day_hist = self._load_previous_day_histogram()
        self.counter.state.histogram_10min = [0] * 144

    def process_once(self) -> dict[str, Any]:
        start = time.time()
        if self.cap is None or not self.cap.isOpened():
            self.connect()
        ok, frame = self.cap.read()
        if not ok:
            time.sleep(float(self.camera_cfg.get("reconnect_sec", 3)))
            self.connect()
            return self._empty_payload()

        now = datetime.now()
        self._rollover_if_needed(now)

        result = self.model.track(
            source=frame,
            persist=True,
            tracker="bytetrack.yaml",
            verbose=False,
            device=self.device,
            conf=0.25,
            classes=None,
        )[0]

        boxes = result.boxes
        tracks: list[dict[str, Any]] = []
        pass_events: list[dict[str, Any]] = []
        long_stay_events: list[dict[str, Any]] = []
        long_stay_list: list[tuple[int, float]] = []
        exclude_polygon = self.camera_cfg.get("exclude_polygon", [])
        stay_zone = self.camera_cfg.get("stay_zone_polygon", []) or exclude_polygon

        if boxes is not None and boxes.id is not None:
            cls_array = boxes.cls.cpu().numpy() if boxes.cls is not None else []
            for i, (box, tid) in enumerate(zip(boxes.xyxy.cpu().numpy(), boxes.id.cpu().numpy())):
                cls_idx = int(cls_array[i]) if len(cls_array) > i else -1
                cls_name = self.model.names.get(cls_idx, str(cls_idx)) if isinstance(self.model.names, dict) else str(cls_idx)
                if cls_name not in self.target_classes:
                    continue

                x1, y1, x2, y2 = box.tolist()
                cx = (x1 + x2) / 2
                cy = (y1 + y2) / 2
                if self._in_polygon((cx, cy), exclude_polygon):
                    continue

                track_id = int(tid)
                tracks.append({
                    "track_id": track_id,
                    "center": (cx, cy),
                    "bbox": (int(x1), int(y1), int(x2), int(y2)),
                    "class_name": cls_name,
                })

                event = self.counter.update(track_id, (cx, cy), cls_name, now)
                if event:
                    pass_events.append(event)

                if track_id not in self.track_state.first_seen:
                    self.track_state.first_seen[track_id] = now

                if self._in_polygon((cx, cy), stay_zone):
                    stay_mins = (now - self.track_state.first_seen[track_id]).total_seconds() / 60.0
                    if stay_mins >= float(self.camera_cfg.get("long_stay_minutes", 15)):
                        long_stay_list.append((track_id, stay_mins))
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

        congestion_score = self.congestion.update(tracks)
        threshold = float(self.camera_cfg.get("congestion_threshold", 60))
        threshold_over = congestion_score >= threshold

        elapsed = max(1e-6, time.time() - start)
        self.fps = 1.0 / elapsed

        for pe in pass_events:
            self._append_csv(self.pass_csv, [
                pe["timestamp"], self.camera_id, pe["track_id"], pe["class_name"], pe["direction"],
            ])

        for le in long_stay_events:
            self._append_csv(self.long_stay_csv, [
                le["first_seen"], le["detected_at"], le["camera_id"], le["track_id"], le["stay_minutes"], le["class_name"],
            ])

        self._append_csv(self.realtime_csv, [
            now.strftime("%Y-%m-%d %H:%M:%S"), self.camera_id, self.camera_name, len(tracks),
            round(congestion_score, 2), threshold, int(threshold_over), len(long_stay_list), round(self.fps, 2),
        ])

        self.last_frame = self._draw_overlay(frame.copy(), tracks, long_stay_list)
        long_stay_list.sort(key=lambda x: x[1], reverse=True)

        return {
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "frame": self.last_frame,
            "congestion_score": congestion_score,
            "threshold": threshold,
            "threshold_over": threshold_over,
            "hist_today": self.counter.state.histogram_10min,
            "hist_prev": self.previous_day_hist,
            "long_stays": long_stay_list[:10],
            "long_stay_count": len(long_stay_list),
            "fps": self.fps,
            "device": self.device,
            "gpu_name": self.gpu_name,
        }

    def _draw_overlay(self, frame: np.ndarray, tracks: list[dict[str, Any]], long_stays: list[tuple[int, float]]) -> np.ndarray:
        line = self.camera_cfg.get("line_points", [[10, 10], [100, 10]])
        cv2.line(frame, tuple(line[0]), tuple(line[1]), (0, 255, 255), 2)

        poly = self.camera_cfg.get("exclude_polygon", [])
        if len(poly) >= 3:
            cv2.polylines(frame, [np.array(poly, np.int32)], isClosed=True, color=(255, 0, 255), thickness=2)

        for tr in tracks:
            x1, y1, x2, y2 = tr["bbox"]
            cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 255, 0), 2)
            cv2.putText(
                frame,
                f"ID:{tr['track_id']} {tr['class_name']}",
                (x1, max(20, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 255),
                2,
                cv2.LINE_AA,
            )

        for tid, mins in long_stays:
            cv2.putText(frame, f"LONG STAY ID:{tid} {mins:.1f}m", (20, 40 + 22 * (tid % 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        return frame

    def _empty_payload(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "camera_name": self.camera_name,
            "frame": self.last_frame,
            "congestion_score": 0.0,
            "threshold": float(self.camera_cfg.get("congestion_threshold", 60)),
            "threshold_over": False,
            "hist_today": self.counter.state.histogram_10min,
            "hist_prev": self.previous_day_hist,
            "long_stays": [],
            "long_stay_count": 0,
            "fps": self.fps,
            "device": self.device,
            "gpu_name": self.gpu_name,
        }
