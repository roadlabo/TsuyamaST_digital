# ==================  標準ライブラリのインポート ================== 
import argparse
import os
import csv
import json
import sys
import platform
import re
import inspect
import time
import threading
import traceback
import faulthandler
from contextlib import ContextDecorator
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Optional

# ==================  外部ライブラリのインポート ================== 
import cv2
import torch
import numpy as np
import ultralytics
from ultralytics import YOLO
from ultralytics import cfg as yolo_cfg

_ORIG_CHECK_DICT_ALIGNMENT = yolo_cfg.check_dict_alignment


def _aicount_safe_check_dict_alignment(cfg, overrides=None):
    try:
        if isinstance(overrides, dict):
            unknown = [k for k in list(overrides.keys()) if k not in cfg]
            if unknown:
                print(f"[YOLO cfg patch] Ignore unknown keys: {unknown}")
                for k in unknown:
                    overrides.pop(k, None)
    except Exception as e:
        print(f"[YOLO cfg patch error] {e}")

    return _ORIG_CHECK_DICT_ALIGNMENT(cfg, overrides)


yolo_cfg.check_dict_alignment = _aicount_safe_check_dict_alignment

# Ultralytics/YOLO のバージョンを上げる際は、開発環境で検証したうえで
# requirements での固定バージョンを更新し、未知キーは上記パッチで吸収する運用とする。

try:
    import boxmot  # for version logging
    from boxmot import ByteTrack  # または BYTETracker / ByteTrack のどれか実在するクラス名
    BOXMOT_AVAILABLE = True
    print(f"[BT] boxmot version: {boxmot.__version__}")
except Exception as e:
    BOXMOT_AVAILABLE = False
    ByteTrack = None
    print(f"[BT] boxmot not available: {e}")

try:
    from boxmot.trackers.deepsort.deepsort import DeepSort
except Exception:
    DeepSort = None


if getattr(cv2, "__version__", "").startswith("4."):
    try:
        cv2.imshow
    except Exception:
        raise RuntimeError("OpenCV GUI が無効です。opencv-python>=4.9 をインストールしてください。")


def safe_imshow(win, frame):
    try:
        cv2.imshow(win, frame)
    except cv2.error:
        return

# --- GPU performance knobs (safe defaults) ---
try:
    # OpenCV: avoid oversubscription on CPU side
    try:
        cv2.setNumThreads(1)
    except Exception:
        pass

    if torch.cuda.is_available():
        # cuDNN autotuner (stable input size ⇒ faster)
        torch.backends.cudnn.benchmark = True
        try:
            torch.backends.cudnn.allow_tf32 = True
        except Exception:
            pass
        # Enable TF32 (Ampere+). Harmless on older cards (no-op)
        try:
            torch.backends.cuda.matmul.allow_tf32 = True
        except Exception:
            pass
        # Torch 2.x: prefer higher-throughput matmul kernels
        try:
            torch.set_float32_matmul_precision("high")
        except Exception:
            pass
except Exception:
    pass
from scipy.optimize import linear_sum_assignment
import pytesseract

_DEBUG_MODE = False


def log(message: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"{timestamp} {message}"
    print(text)


def debug_log(message: str):
    if _DEBUG_MODE:
        log(message)


def _dump_threads(tag: str):
    try:
        log(f"[DUMP] dump_traceback tag={tag}")
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    except Exception as exc:
        log(f"[DUMP] failed: {exc}")


class _HangWatchdog:
    def __init__(self, timeout_sec: int, tag: str):
        self.timeout_sec = int(timeout_sec)
        self.tag = tag
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if self.timeout_sec > 0:
            self._th.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        if self._stop.wait(self.timeout_sec):
            return
        _dump_threads(f"{self.tag}_timeout_{self.timeout_sec}s")
        log("[DUMP] Hang detected. Exiting.")
        os._exit(2)

_numpy_version = getattr(np, "__version__", "unknown")
try:
    print(f"[AICount] numpy = {_numpy_version}")
except Exception:
    pass

_detected_cc = None
_force_cpu_due_to_cc = False
try:
    if torch.cuda.is_available():
        _detected_cc = torch.cuda.get_device_capability(0)
        cc_value = _detected_cc[0] * 10 + _detected_cc[1]

        # 古すぎる GPU (Compute Capability < 50) だけ CPU にフォールバックする
        # RTX 5080 (sm_120) など新しい GPU は PyTorch に任せてそのまま使う
        if cc_value < 50:
            print("[AICount] WARNING: GPU compute capability too old. Falling back to CPU.")
            _force_cpu_due_to_cc = True
except Exception:
    pass

_boxmot_version = None
try:
    if boxmot is not None:
        _boxmot_version = getattr(boxmot, "version", None) or getattr(boxmot, "__version__", None)
except Exception:
    _boxmot_version = None


# -------------------------------------------------------------------------
# ByteTrack (boxmot) helper
#   - boxmot の ByteTrack.__init__ シグネチャを実行時に調べ、
#     サポートされている引数だけを渡して初期化する
# -------------------------------------------------------------------------
def create_bytetrack_tracker_from_boxmot(
    cfg: dict,
    device_str: str,
    frame_rate: float,
) -> Optional[object]:
    """
    boxmot 13.x 以降に対応した ByteTrack 初期化ヘルパー
    - config & UI の値からパラメータ候補を作成
    - ByteTrack.__init__ のシグネチャを inspect し、
      該当するキーだけを kwargs として渡す
    - 失敗したら None を返して上位側でフォールバックする
    """
    try:
        import boxmot  # type: ignore
        try:
            # 新 API: from boxmot import ByteTrack
            from boxmot import ByteTrack  # type: ignore
        except Exception:
            # 旧/別構成の場合は trackers パッケージから探す
            try:
                from boxmot.trackers.byte_tracker.byte_tracker import ByteTrack  # type: ignore
            except Exception as e:
                print(f"[BT] Failed to import ByteTrack from boxmot: {e}")
                return None

        print(f"[BT] boxmot version (runtime): {getattr(boxmot, '__version__', 'unknown')}")

        # UI / config から取得する生パラメータ
        # ByteTrack 13.x の __init__:
        #   (min_conf, track_thresh, match_thresh, track_buffer, frame_rate, per_class)
        min_conf     = float(cfg.get("bt_track_low_thresh", 0.1))   # → min_conf
        track_thresh = float(cfg.get("bt_track_high_thresh", 0.5))  # → track_thresh
        match_thresh = float(cfg.get("bt_match_thresh", 0.6))       # → match_thresh
        track_buffer = int(cfg.get("bt_track_buffer", 30))          # → track_buffer

        # ByteTrack に渡すパラメータを必要十分な形に整理
        candidate_kwargs = {
            "min_conf":     min_conf,
            "track_thresh": track_thresh,
            "match_thresh": match_thresh,
            "track_buffer": track_buffer,
            "frame_rate":   float(frame_rate),
            # per_class は現状 UI を設けず、ライブラリのデフォルトに任せる
        }

        # ByteTrack.__init__ のシグネチャを introspection
        sig = inspect.signature(ByteTrack.__init__)
        valid_keys = sig.parameters.keys()

        # サポートされているキーだけにフィルタリング
        filtered_kwargs = {
            k: v for k, v in candidate_kwargs.items()
            if k in valid_keys and v is not None
        }

        print(f"[BT] ByteTrack.__init__ parameters = {list(valid_keys)}")
        print(f"[BT] ByteTrack kwargs (filtered) = {filtered_kwargs}")

        tracker = ByteTrack(**filtered_kwargs)
        print("[BT] ByteTrack initialized successfully via dynamic kwargs.")
        return tracker

    except Exception as e:
        print(f"[BT] ByteTrack initialization failed (dynamic): {e}")
        return None


def _log_environment_info():
    try:
        python_ver = platform.python_version()
        torch_ver = getattr(torch, "__version__", "unknown")
        cuda_available = torch.cuda.is_available()
        cc_value = _detected_cc[0] * 10 + _detected_cc[1] if _detected_cc else None
        cc_text = f"sm_{cc_value:03d}" if cc_value is not None else "N/A"
        print(f"[AICount] Python = {python_ver}")
        print(f"[AICount] torch = {torch_ver} cuda={cuda_available}")
        print(f"[AICount] GPU capability = {cc_text}")
        print(f"[AICount] OpenCV = {getattr(cv2, '__version__', 'unknown')}")
        print(f"[AICount] boxmot = {_boxmot_version or 'unavailable'}")
    except Exception:
        pass


_log_environment_info()
print(f"[AICount] torch = {torch.__version__}, cuda={torch.cuda.is_available()}")
print(f"[AICount] GPU capability detected = {torch.cuda.get_device_capability(0) if torch.cuda.is_available() else 'CPU'}")

# --- RoadLabo license gate (JST hard stop from 2027-04-01) ---
ROADLABO_URL = "https://roadlabo.com/"


def _roadlabo_check_expiry():
    """Stop analyzer after 2027-04-01 00:00:00 JST."""

    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    cutoff = datetime(2027, 4, 1, 0, 0, 0, tzinfo=jst)
    if now >= cutoff:
        msg = (
            "利用期限を超過しました。\n"
            "引き続きご使用される場合はRoadLaboにお問い合わせください。\n"
            f"{ROADLABO_URL}"
        )
        # 1) 可能ならGUIダイアログで通知
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb

            root = _tk.Tk()
            root.withdraw()
            _mb.showerror("AICount 利用期限", msg)
            try:
                root.destroy()
            except Exception:
                pass
        except Exception:
            # 2) GUI不可なら標準出力に通知
            print("\n=== 利用期限超過 ===")
            print(msg)
            print("====================\n")
        # 3) いかなる状況でも即停止
        import sys

        sys.exit(2)


# 実行直後にチェック（設定ダイアログ等よりも前に止める）
_roadlabo_check_expiry()

SCRIPT_DIR = Path(__file__).resolve().parent

runtime_device: str = "unknown"
runtime_device_type: str = "unknown"
is_gpu_runtime: bool = False
is_cpu_fallback: bool = False
device_text: str = "Device: CPU"
env_info: dict = {}


def _runtime_device_texts() -> list[str]:
    """オーバーレイ用：CPU/GPU/torchの実行環境情報テキストを返す（printしない）"""

    lines = []
    try:
        torch_ver = getattr(torch, "__version__", "unknown")
        cuda_rt = getattr(torch.version, "cuda", "unknown") if is_gpu_runtime else ""

        lines.append(device_text)
        lines.append(f"torch {torch_ver}" + (f" / CUDA {cuda_rt}" if is_gpu_runtime else ""))
        # 2) 最適化ヒント（有効/無効を示すだけ）
        try:
            tf32_cuda = getattr(torch.backends.cuda.matmul, "allow_tf32", False)
        except Exception:
            tf32_cuda = False
        try:
            tf32_cudnn = getattr(torch.backends.cudnn, "allow_tf32", False)
        except Exception:
            tf32_cudnn = False
        try:
            prec = torch.get_float32_matmul_precision()
        except Exception:
            prec = "default"
        hints = []
        if is_gpu_runtime and tf32_cuda:
            hints.append("TF32(matmul)")
        if is_gpu_runtime and tf32_cudnn:
            hints.append("TF32(cuDNN)")
        if prec:
            hints.append(f"matmul:{prec}")
        if hints:
            lines.append("Perf: " + ", ".join(hints))
    except Exception:
        # 取得失敗時も描画系は止めない
        pass
    return lines


CONFIG_DEFAULTS = {
    "crossing_system": 1,
    "line_start": [1276, 491],
    "line_end": [5, 294],
    "p1": [1912, 639],
    "p2": [396, 681],
    "p3": [679, 435],
    "p4": [1446, 440],
    "exclude_polygon": [(3, 3), (1276, 3), (1278, 313), (1, 129)],
    "yolo11_size": "x",
    "yolo_weights_path": "",
    "yolo_device": "auto",
    "target_classes": [2, 3, 5, 7],
    "confidence_threshold": 0.05,
    "distance_threshold": 200,
    "frame_skip": 2,
    "frame_rate_original": 5,
    "std_acc": 1.0,
    "x_std_meas": 1.0,
    "y_std_meas": 1.0,
    "timestamp_ocr": False,
    "ocr_box": [906, 689, 1275, 719],
    "show_estimated_time": False,
    "start_time_str": "2025-01-01 16:00:00",
    "end_time_str": "2025-01-01 16:03:12",
    "preview": True,
    "video_dir": "",
    "output_dir": "",
    "congestion_calculation_interval": 10,
    "iou_threshold": 0.2,
    "max_iou_distance": 0.7,
    "max_age": 30,
    "n_init": 3,
    "nn_budget": 100,
    "tracking_method": "bytetrack",
    "search_subdirs": False,
    "bt_track_high_thresh": 0.3,
    "bt_track_low_thresh": 0.01,
    "bt_new_track_thresh": 0.5,
    "bt_match_thresh": 0.9,
    "bt_track_buffer": 100,
}


TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _safe_env_value(func, default=""):
    try:
        value = func()
        if value is None:
            return default
        return value
    except Exception:
        return default


def _format_timestamp_with_seconds(dt: datetime | None, fallback: str = "") -> str:
    """Format datetime for CSV/logging with second precision."""

    if dt is None:
        return fallback

    try:
        if dt.year == 2000 and dt.month == 1 and dt.day == 1:
            return dt.strftime("%H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return fallback


_DATE_KEY_REGEX = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _extract_date_key_from_timestr(time_value: str) -> str:
    """
    'YYYY-MM-DD HH:MM:SS' 形式などの文字列から日付キー 'YYYYMMDD' を取り出す。
    取り出せない場合は 'nodate' を返す。
    """

    if not isinstance(time_value, str):
        return "nodate"

    m = _DATE_KEY_REGEX.search(time_value)
    if not m:
        return "nodate"

    y, m_, d = m.groups()
    return f"{y}{m_}{d}"


def _collect_env_info() -> dict:
    info = {
        "runtime_device": runtime_device_type or runtime_device or "unknown",
        "cpu_fallback": is_cpu_fallback,
        "yolo_device": _safe_str(globals().get("yolo_device_config", ""), ""),
        "yolo_model": Path(globals().get("yolo_weights", "")).stem,
        "tracking_method": globals().get("tracking_method", ""),
        "torch_version": _safe_env_value(lambda: getattr(torch, "__version__", ""), "unknown"),
        "cuda_version": _safe_env_value(lambda: getattr(torch.version, "cuda", ""), ""),
        "opencv_version": _safe_env_value(lambda: getattr(cv2, "__version__", ""), ""),
        "numpy_version": _safe_env_value(lambda: getattr(np, "__version__", ""), ""),
        "boxmot_version": _safe_env_value(
            lambda: getattr(globals().get("boxmot"), "__version__", globals().get("_boxmot_version", "")),
            "",
        ),
        "ultralytics_version": _safe_env_value(
            lambda: ultralytics.__version__()
            if callable(getattr(ultralytics, "__version__", None))
            else getattr(ultralytics, "__version__", ""),
            "",
        ),
        "python_version": _safe_env_value(lambda: sys.version.replace("\n", " "), "unknown"),
        "os": _safe_env_value(lambda: f"{platform.system()}-{platform.release()}", "unknown"),
    }

    info["gpu_name"] = _safe_env_value(lambda: torch.cuda.get_device_name(0), "") if is_gpu_runtime else ""
    return info


def pick_device(force_cpu: bool = False) -> str:
    """Return the preferred torch device based on availability and a CPU override."""

    if force_cpu:
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


def _safe_int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _safe_bool(value, fallback):
    if isinstance(value, bool):
        return value
    return fallback


def _safe_str(value, fallback):
    if isinstance(value, str):
        return value
    return fallback


def _parse_datetime_with_optional_date(value: str, label: str) -> datetime:
    """Parse date+time or time-only strings into a datetime object."""

    value = _safe_str(value, "").strip()
    if not value:
        raise ValueError(f"{label} が空です。")

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass

    try:
        t = datetime.strptime(value, "%H:%M:%S").time()
        return datetime(2000, 1, 1, t.hour, t.minute, t.second)
    except ValueError as exc:
        raise ValueError(
            f"{label} は 'YYYY-MM-DD HH:MM:SS' または 'HH:MM:SS' 形式で指定してください。入力値: {value}\n{exc}"
        ) from exc


def _to_point(value, fallback):
    try:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return tuple(int(v) for v in value)
    except (TypeError, ValueError):
        pass
    return fallback


def _to_box(value, fallback):
    try:
        if isinstance(value, (list, tuple)) and len(value) == 4:
            return tuple(int(v) for v in value)
    except (TypeError, ValueError):
        pass
    return fallback


def _to_polygon(value, fallback):
    if isinstance(value, (list, tuple)):
        converted = []
        for pt in value:
            try:
                if isinstance(pt, (list, tuple)) and len(pt) == 2:
                    converted.append((int(pt[0]), int(pt[1])))
                else:
                    return fallback
            except (TypeError, ValueError):
                return fallback
        return converted
    return fallback


def _to_int_list(value, fallback):
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                return fallback
        return result
    return fallback


def _get_color_name(color_value, default="N/A"):
    """Return a human readable color name for a BGR tuple."""

    color_map = {
        (0, 0, 255): "Red",
        (255, 0, 0): "Blue",
        (0, 255, 255): "Yellow",
        (255, 0, 255): "Purple",
        (0, 255, 0): "Green",
        (255, 255, 255): "White",
        (0, 0, 0): "Black",
    }
    try:
        key = tuple(int(v) for v in color_value)
    except (TypeError, ValueError):  # pragma: no cover - defensive fallback
        return default
    return color_map.get(key, default)


def _resolve_path(value, base_dir, fallback):
    """Return an absolute Path from a config value."""

    candidate = None
    if isinstance(value, str) and value.strip():
        candidate = value.strip()
    elif isinstance(fallback, (str, Path)):
        candidate = fallback
    else:
        candidate = str(fallback)

    path = Path(candidate)
    if not path.is_absolute():
        path = (Path(base_dir) / path).resolve()
    return path


def _parse_command_line_arguments():
    parser = argparse.ArgumentParser(description="AICount analyzer")
    parser.add_argument("--config", dest="config_path", help="解析設定ファイルのパス", default=None)
    parser.add_argument("--config-json", dest="config_json", help="解析設定JSON文字列", default=None)
    parser.add_argument(
        "--probe-device",
        action="store_true",
        help="print device info (JSON) and exit",
    )
    # 追加: 実行デバイスの指定（auto / cpu / cuda / cuda:0 等）
    parser.add_argument(
        "--device",
        dest="device",
        help="実行デバイスを指定 (auto / cpu / cuda / cuda:<idx>)",
        default="auto"
    )
    parser.add_argument(
        "--yolo-size",
        dest="yolo_size",
        default="m",
        choices=["n", "s", "m", "l", "x"],
        help="YOLOv11 のモデルサイズ"
    )
    parser.add_argument(
        "--weights",
        dest="weights_path",
        default=None,
        help="学習済み重みのパス（未指定なら公式 yolo11{size}.pt）",
    )
    parser.add_argument(
        "--skip-model-to",
        action="store_true",
        help="model.to をスキップする (デバッグ用)",
    )
    parser.add_argument(
        "--yolo-device-override",
        type=str,
        default=None,
        help="YOLO 推論デバイスを強制上書きする",
    )
    parser.add_argument(
        "--to-timeout-sec",
        type=int,
        default=40,
        help="model.to のハング監視タイムアウト秒",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="デバッグログを有効化する",
    )
    parser.add_argument(
        "--debug-log",
        dest="debug_log",
        action="store_true",
        help="デバッグログを有効化する (互換オプション)",
    )
    return parser.parse_args()


class KalmanFilter:
    """Simple constant-velocity Kalman filter used by the Hungarian tracker."""

    def __init__(self, dt, u_x, u_y, std_acc, x_std_meas, y_std_meas):
        self.dt = dt
        self.u = np.matrix([[u_x], [u_y], [0], [0]])
        self.F = np.matrix(
            [
                [1, 0, dt, 0],
                [0, 1, 0, dt],
                [0, 0, 1, 0],
                [0, 0, 0, 1],
            ]
        )
        self.B = np.matrix([[0.5 * dt ** 2], [0.5 * dt ** 2], [dt], [dt]])
        self.H = np.matrix([[1, 0, 0, 0], [0, 1, 0, 0]])
        self.Q = np.matrix(
            [
                [0.5 * dt ** 4, 0, dt ** 3, 0],
                [0, 0.5 * dt ** 4, 0, dt ** 3],
                [dt ** 3, 0, dt ** 2, 0],
                [0, dt ** 3, 0, dt ** 2],
            ]
        ) * (std_acc ** 2)
        self.R = np.matrix([[x_std_meas ** 2, 0], [0, y_std_meas ** 2]])
        self.P = np.eye(self.F.shape[1])

    def predict(self):
        self.u = np.dot(self.F, self.u)
        self.P = np.dot(np.dot(self.F, self.P), self.F.T) + self.Q
        return self.u[:2]

    def update(self, z):
        y = z - np.dot(self.H, self.u)
        S = np.dot(self.H, np.dot(self.P, self.H.T)) + self.R
        K = np.dot(np.dot(self.P, self.H.T), np.linalg.inv(S))
        self.u = self.u + np.dot(K, y)
        I = np.eye(self.H.shape[1])
        self.P = (I - np.dot(K, self.H)) * self.P


def assign_ids_with_hungarian(detections, tracked_vehicles, distance_threshold, confidence_threshold=0.5):
    """Assign IDs to detections using the Hungarian algorithm."""

    filtered_detections = [d for d in detections if d.get("confidence", 1.0) >= confidence_threshold]
    num_detections = len(filtered_detections)
    num_tracked = len(tracked_vehicles)

    if num_detections == 0 or num_tracked == 0:
        return {}

    cost_matrix = np.zeros((num_tracked, num_detections))

    for i, (track_id, track_data) in enumerate(tracked_vehicles.items()):
        pred_x, pred_y = track_data["kalman_filter"].predict().A1
        for j, detection in enumerate(filtered_detections):
            det_x, det_y = detection["center"]
            distance = np.sqrt((pred_x - det_x) ** 2 + (pred_y - det_y) ** 2)
            if distance < distance_threshold:
                cost_matrix[i, j] = distance
            else:
                cost_matrix[i, j] = 1e6

    try:
        row_indices, col_indices = linear_sum_assignment(cost_matrix)
    except ValueError as exc:  # pragma: no cover - defensive programming
        print(f"Error during assignment: {exc}")
        return {}

    assignments = {}
    for row, col in zip(row_indices, col_indices):
        cost = cost_matrix[row, col]
        if cost < distance_threshold:
            track_id = list(tracked_vehicles.keys())[row]
            assignments[col] = track_id

    assigned_detections = set(assignments.keys())
    unassigned_detections = set(range(num_detections)) - assigned_detections
    for unassigned in unassigned_detections:
        assignments[unassigned] = None

    return assignments


class HungarianTracker:
    def __init__(self, distance_threshold, max_age):
        self.distance_threshold = distance_threshold
        self.max_age = max_age


def compute_iou_xyxy(box1, box2):
    """Compute IoU for boxes expressed as (x1, y1, x2, y2)."""

    x1, y1, x2, y2 = box1
    x1_, y1_, x2_, y2_ = box2

    xi1 = max(x1, x1_)
    yi1 = max(y1, y1_)
    xi2 = min(x2, x2_)
    yi2 = min(y2, y2_)
    inter_area = max(0, xi2 - xi1) * max(0, yi2 - yi1)

    box1_area = max(0, x2 - x1) * max(0, y2 - y1)
    box2_area = max(0, x2_ - x1_) * max(0, y2_ - y1_)

    union_area = box1_area + box2_area - inter_area
    return inter_area / union_area if union_area > 0 else 0.0


def load_analysis_config(config_path=None, preloaded_config=None):
    """Load analysis parameters either from a file, inline JSON, or via dialog."""

    loaded = None
    if preloaded_config is not None:
        if not isinstance(preloaded_config, dict):
            raise TypeError("preloaded_config must be a dictionary")
        loaded = preloaded_config
    elif config_path:
        config_path = os.path.abspath(config_path)
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as exc:  # noqa: BLE001 - show error dialog and exit
            messagebox.showerror("設定ファイル読込エラー", f"設定ファイルの読み込みに失敗しました。\n{exc}")
            raise SystemExit(1) from exc
    else:
        root = tk.Tk()
        root.withdraw()
        selected_path = filedialog.askopenfilename(
            title="解析設定ファイルを選択",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile="analyzer_config.json",
        )
        if not selected_path:
            messagebox.showerror("設定ファイル未選択", "解析設定ファイルが選択されなかったため終了します。")
            root.destroy()
            raise SystemExit(1)
        config_path = selected_path
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as exc:  # noqa: BLE001 - show error dialog and exit
            messagebox.showerror("設定ファイル読込エラー", f"設定ファイルの読み込みに失敗しました。\n{exc}")
            root.destroy()
            raise SystemExit(1)
        root.destroy()

    config = CONFIG_DEFAULTS.copy()
    if isinstance(loaded, dict):
        config.update(loaded)

    resolved_path = os.path.abspath(config_path) if config_path else "<inline_config>"
    return config, resolved_path


def _coerce_numeric(value, default, caster):
    try:
        if value is None:
            raise ValueError("value is None")
        return caster(value)
    except Exception:
        return caster(default)


# NOTE:
# torch 2.1+ emits a FutureWarning when torch.cuda.amp.autocast is used directly.
# We monkey patch the CUDA autocast context manager to delegate to the new
# torch.amp.autocast implementation (when available) so the existing YOLOv5 code
# continues to run without warnings on newer versions while remaining compatible
# with older torch releases that still expect the legacy API.


def _patch_cuda_autocast():
    """Shim torch.cuda.amp.autocast to the new torch.amp API when available."""

    cuda_amp = getattr(torch.cuda, "amp", None)
    torch_amp = getattr(torch, "amp", None)
    if not (cuda_amp and torch_amp and hasattr(torch_amp, "autocast")):
        return

    if not hasattr(cuda_amp, "autocast") or getattr(_patch_cuda_autocast, "_patched", False):
        return

    try:
        torch_amp.autocast("cuda")
    except TypeError:
        # torch.amp.autocast does not support the torch>=2 style invocation.
        return

    class _CudaAutocastProxy(ContextDecorator):
        def __init__(self, *args, **kwargs):
            """Translate legacy torch.cuda.amp.autocast arguments to torch.amp."""

            legacy_args = list(args)

            enabled = kwargs.pop("enabled", None)
            dtype = kwargs.pop("dtype", None)
            cache_enabled = kwargs.pop("cache_enabled", None)

            # torch.cuda.amp.autocast historically accepted positional arguments in the
            # order (enabled, dtype, cache_enabled).  Newer torch.amp.autocast expects
            # (device_type, *, enabled=True, dtype=None, cache_enabled=True).  We
            # normalise the legacy positional arguments here so that calls like
            # torch.cuda.amp.autocast(True) continue to work.
            if legacy_args:
                first = legacy_args.pop(0)
                if isinstance(first, bool) or first is None:
                    if enabled is None:
                        enabled = first
                else:
                    if dtype is None:
                        dtype = first

            if legacy_args:
                second = legacy_args.pop(0)
                if isinstance(second, torch.dtype) or second is None:
                    if dtype is None:
                        dtype = second
                elif isinstance(second, bool):
                    if cache_enabled is None:
                        cache_enabled = second

            if legacy_args:
                third = legacy_args.pop(0)
                if isinstance(third, bool) or third is None:
                    if cache_enabled is None:
                        cache_enabled = third
                elif isinstance(third, torch.dtype) and dtype is None:
                    dtype = third

            kwargs_new = {}
            if dtype is not None:
                kwargs_new["dtype"] = dtype
            if enabled is not None:
                kwargs_new["enabled"] = enabled
            if cache_enabled is not None:
                kwargs_new["cache_enabled"] = cache_enabled

            self._ctx = torch_amp.autocast("cuda", **kwargs_new)

        def __enter__(self):
            return self._ctx.__enter__()

        def __exit__(self, exc_type, exc, tb):
            return self._ctx.__exit__(exc_type, exc, tb)

    cuda_amp.autocast = _CudaAutocastProxy
    _patch_cuda_autocast._patched = True


_patch_cuda_autocast()


_cli_args = _parse_command_line_arguments()
_DEBUG_MODE = bool(
    getattr(_cli_args, "debug", False)
    or getattr(_cli_args, "debug_log", False)
)
if getattr(_cli_args, "probe_device", False):
    import json as _json  # noqa: WPS433 - deliberate local import for quick exit
    import torch as _torch  # noqa: WPS433 - deliberate local import for quick exit

    _info = {
        "torch": getattr(_torch, "__version__", "unknown"),
        "cuda": bool(_torch.cuda.is_available()),
        "gpu_name": None,
        "tf32": False,
        "cudnn_benchmark": False,
    }
    try:  # noqa: WPS229 - nested defensive probes
        if _info["cuda"]:
            _idx = _torch.cuda.current_device()
            _prop = _torch.cuda.get_device_properties(_idx)
            _info["gpu_name"] = getattr(_prop, "name", None)
        try:
            _info["tf32"] = bool(getattr(_torch.backends.cuda.matmul, "allow_tf32", False))
        except Exception:
            pass
        try:
            _info["cudnn_benchmark"] = bool(getattr(_torch.backends.cudnn, "benchmark", False))
        except Exception:
            pass
    except Exception:
        pass

    print(_json.dumps(_info, ensure_ascii=False))
    raise SystemExit(0)

_preloaded_config = None
if _cli_args.config_json:
    try:
        _preloaded_config = json.loads(_cli_args.config_json)
    except json.JSONDecodeError as exc:  # noqa: PERF203 - immediate exit on error
        print(f"Failed to parse --config-json: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

analysis_config, analysis_config_path = load_analysis_config(
    config_path=_cli_args.config_path,
    preloaded_config=_preloaded_config,
)
print(f"設定ファイル: {analysis_config_path}")
analysis_config_dir = os.path.dirname(analysis_config_path) or "."

tracker_defaults = {
    "bt_track_high_thresh": CONFIG_DEFAULTS["bt_track_high_thresh"],
    "bt_track_low_thresh": CONFIG_DEFAULTS["bt_track_low_thresh"],
    "bt_new_track_thresh": CONFIG_DEFAULTS["bt_new_track_thresh"],
    "bt_match_thresh": CONFIG_DEFAULTS["bt_match_thresh"],
    "bt_track_buffer": CONFIG_DEFAULTS["bt_track_buffer"],
    "max_iou_distance": CONFIG_DEFAULTS["max_iou_distance"],
    "max_age": CONFIG_DEFAULTS["max_age"],
    "n_init": CONFIG_DEFAULTS["n_init"],
    "nn_budget": CONFIG_DEFAULTS["nn_budget"],
}

for key, default in tracker_defaults.items():
    caster = float if "distance" in key or "thresh" in key else int
    analysis_config[key] = _coerce_numeric(analysis_config.get(key), default, caster)

for key in CONFIG_DEFAULTS:
    assert key in analysis_config, f"Missing config key: {key}"

for k, v in analysis_config.items():
    print(f"[config] {k} = {v}")

_cli_device = getattr(_cli_args, "device", "auto")
yolo_device_config = _safe_str(analysis_config.get("yolo_device"), CONFIG_DEFAULTS["yolo_device"]).lower()
if isinstance(_cli_device, str):
    _cli_device = _cli_device.strip().lower()
    if _cli_device and _cli_device != "auto":
        yolo_device_config = _cli_device

if _force_cpu_due_to_cc:
    yolo_device_config = "cpu"

# Respect UI-configured size unless the CLI explicitly overrides it
_cli_overrode_size = any(arg == "--yolo-size" or arg.startswith("--yolo-size=") for arg in sys.argv[1:])
explicit_cli_size = _cli_args.yolo_size if _cli_overrode_size else None
model_size = (
    explicit_cli_size
    or analysis_config.get("yolo11_size")
    or CONFIG_DEFAULTS["yolo11_size"]
).lower()
if model_size not in {"n", "s", "m", "l", "x"}:
    model_size = CONFIG_DEFAULTS["yolo11_size"]
weights_path_cli = getattr(_cli_args, "weights_path", None)
weights_path_config = analysis_config.get("yolo_weights_path")
weights_path_value = weights_path_cli if weights_path_cli else weights_path_config
weights_path_value = weights_path_value or ""

force_cpu = _force_cpu_due_to_cc or (yolo_device_config == "cpu")
yolo_device = pick_device(force_cpu=force_cpu)
if getattr(_cli_args, "yolo_device_override", None):
    yolo_device = _cli_args.yolo_device_override.strip()
    log(f"[CHK] yolo_device overridden by CLI: {yolo_device}")
_cuda_index = None
if yolo_device_config.startswith("cuda") and ":" in yolo_device_config:
    try:
        _cuda_index = int(yolo_device_config.split(":", 1)[1])
    except Exception:
        _cuda_index = None
if yolo_device == "cuda" and _cuda_index is not None:
    try:
        torch.cuda.set_device(_cuda_index)
    except Exception:
        pass
if yolo_device != "cuda":
    os.environ.pop("PYTORCH_CUDA_ALLOC_CONF", None)

weights_path_value = weights_path_value.strip() if isinstance(weights_path_value, str) else ""
if weights_path_value:
    resolved_weights = _resolve_path(weights_path_value, analysis_config_dir, weights_path_value)
    if resolved_weights.exists():
        yolo_weights = str(resolved_weights)
    else:
        messagebox.showerror("YOLO 重み未検出", f"指定された YOLO の重みファイルが存在しません:\n{resolved_weights}")
        raise SystemExit(1)
else:
    yolo_weights = f"yolo11{model_size}.pt"

print(f"YOLO weights: {yolo_weights}")
print(f"YOLO device: {yolo_device}")
try:
    model = YOLO(yolo_weights)
except Exception as exc:
    messagebox.showerror("YOLOv11 読込エラー", f"YOLOv11 の読み込みに失敗しました。\n{exc}")
    raise SystemExit(1)

if getattr(_cli_args, "skip_model_to", False):
    log("[CHK] Skip model.to(device) due to --skip-model-to")
else:
    log(f"[CHK] model.to({yolo_device}) start")
    wd = _HangWatchdog(getattr(_cli_args, "to_timeout_sec", 40), tag="model_to")
    wd.start()
    t0 = time.time()
    try:
        model.to(yolo_device)
    except Exception as e:
        log(f"[ERR] model.to({yolo_device}) raised: {repr(e)}")
        _dump_threads("model_to_exception")
        raise SystemExit(2)
    finally:
        wd.stop()
    log(f"[CHK] model.to({yolo_device}) done in {time.time()-t0:.2f}s")


runtime_device_obj = getattr(model, "device", None)
runtime_device = str(runtime_device_obj) if runtime_device_obj is not None else "unknown"
runtime_device_type = getattr(runtime_device_obj, "type", runtime_device)
is_gpu_runtime = runtime_device_type != "cpu"
intended_gpu = (
    yolo_device_config in {"auto", "cuda", "gpu"}
    or yolo_device_config.startswith("cuda")
    or yolo_device_config.startswith("gpu")
)
is_cpu_fallback = (not is_gpu_runtime) and intended_gpu
log(
    f"[CHK] YOLO ready: weights={yolo_weights}, yolo_device={yolo_device}, "
    f"runtime_device={runtime_device}, is_cpu_fallback={is_cpu_fallback}"
)

if is_gpu_runtime:
    gpu_name = _safe_env_value(lambda: torch.cuda.get_device_name(0), "GPU")
    device_text = f"Device: CUDA {gpu_name}"
elif is_cpu_fallback:
    device_text = f"Device: CPU (fallback from '{yolo_device_config}')"
else:
    device_text = "Device: CPU"


model_names = getattr(model, "names", {})
if isinstance(model_names, list):
    model_names = {i: name for i, name in enumerate(model_names)}

try:
    if hasattr(model, "fuse"):
        model.fuse()
except Exception:
    pass

# ================== 解析パラメータ設定セクション ==================

# DeepSORT parameters (従来どおり)
max_iou_distance = _safe_float(
    analysis_config.get("max_iou_distance"), CONFIG_DEFAULTS["max_iou_distance"]
)
max_age = _safe_int(analysis_config.get("max_age"), CONFIG_DEFAULTS["max_age"])
n_init = _safe_int(analysis_config.get("n_init"), CONFIG_DEFAULTS["n_init"])
nn_budget = _safe_int(analysis_config.get("nn_budget"), CONFIG_DEFAULTS["nn_budget"])

# ByteTrack parameters (UI から JSON 経由で渡される)
bt_track_high_thresh = _safe_float(
    analysis_config.get("bt_track_high_thresh"), CONFIG_DEFAULTS["bt_track_high_thresh"]
)
bt_track_low_thresh = _safe_float(
    analysis_config.get("bt_track_low_thresh"), CONFIG_DEFAULTS["bt_track_low_thresh"]
)
bt_new_track_thresh = _safe_float(
    analysis_config.get("bt_new_track_thresh"), CONFIG_DEFAULTS["bt_new_track_thresh"]
)
bt_match_thresh = _safe_float(
    analysis_config.get("bt_match_thresh"), CONFIG_DEFAULTS["bt_match_thresh"]
)
bt_track_buffer = _safe_int(
    analysis_config.get("bt_track_buffer"), CONFIG_DEFAULTS["bt_track_buffer"]
)

tracking_method = _safe_str(
    analysis_config.get("tracking_method"), CONFIG_DEFAULTS["tracking_method"]
).lower()

env_info = _collect_env_info()

distance_threshold = _safe_float(
    analysis_config.get("distance_threshold"), CONFIG_DEFAULTS["distance_threshold"]
)

# === フレームスキップの設定 ===
frame_skip = _safe_int(analysis_config.get("frame_skip"), CONFIG_DEFAULTS["frame_skip"])  # 例: 5フレーム毎に解析を実行、スキップしないときは1
if frame_skip <= 0:
    frame_skip = 1

frame_rate_original = _safe_float(
    analysis_config.get("frame_rate_original"), CONFIG_DEFAULTS["frame_rate_original"]
)
frame_rate = frame_rate_original / frame_skip if frame_skip else frame_rate_original
if frame_rate <= 0:
    frame_rate = 1.0
dt = 1.0 / frame_rate

# === UI 設定からの主要なしきい値 ===
# YOLOv11の信頼度しきい値設定（推奨：0.2～0.5の範囲で調整）
confidence_threshold = _safe_float(
    analysis_config.get("confidence_threshold"),
    CONFIG_DEFAULTS.get("confidence_threshold", 0.5),
)
# IoU（Intersection over Union、重複度合い）の閾値
iou_threshold = _safe_float(
    analysis_config.get("iou_threshold"),
    CONFIG_DEFAULTS.get("iou_threshold", 0.8),
)
# トラッカーの寿命（類似概念）
max_age_config = _safe_int(
    analysis_config.get("max_age"), CONFIG_DEFAULTS.get("max_age", 30)
)
max_age_config = max_age

tracker: object | None
if tracking_method == "hungarian":
    tracker = HungarianTracker(distance_threshold=distance_threshold, max_age=max_age)
elif tracking_method == "deepsort":
    if DeepSort is None:
        print("DeepSORT tracker is not available. Falling back to Hungarian tracking.")
        tracking_method = "hungarian"
        tracker = HungarianTracker(distance_threshold=distance_threshold, max_age=max_age)
    else:
        try:
            tracker = DeepSort(
                max_iou_distance=max_iou_distance,
                max_age=max_age,
                n_init=n_init,
                nn_budget=nn_budget,
                device=yolo_device,
            )
            print(
                "[DeepSORT] initialized",
                {
                    "max_iou_distance": max_iou_distance,
                    "max_age": max_age,
                    "n_init": n_init,
                    "nn_budget": nn_budget,
                    "device": yolo_device,
                },
            )
        except Exception as exc:
            print(f"DeepSORT tracker initialization failed: {exc}")
            tracking_method = "hungarian"
            tracker = HungarianTracker(distance_threshold=distance_threshold, max_age=max_age)
elif tracking_method == "bytetrack":
    # ------------------------------------------------------------------
    # ByteTrack (boxmot) 初期化
    #   - config_ui11.py で指定した bt_* パラメータを使用
    #   - 実際に ByteTrack.__init__ が受け付ける引数だけを動的に選択
    # ------------------------------------------------------------------
    print("[AICount] Tracking method = ByteTrack (boxmot).")

    # デバイス文字列（例: "cuda:0" / "cpu"）は YOLO と同じものを使う想定
    # すでに yolo_device / device などが決まっているはずなので、それを再利用する
    if yolo_device == "cuda" or yolo_device == "gpu":
        bt_device_str = "cuda"
    elif isinstance(yolo_device, str):
        bt_device_str = yolo_device
    else:
        bt_device_str = "cpu"

    # frame_skip を考慮して計算済みの frame_rate をそのまま ByteTrack に渡す
    tracker = create_bytetrack_tracker_from_boxmot(
        analysis_config,
        device_str=bt_device_str,
        frame_rate=frame_rate,
    )

    if tracker is None:
        print("[BT] ByteTrack unavailable or initialization failed. Falling back to Hungarian tracker.")
        tracking_method = "hungarian"
        tracker = HungarianTracker(distance_threshold=distance_threshold, max_age=max_age)
else:
    tracker = None

# クロス判定の方法を選択
# パターン1: 1本の線でLtoRとRtoLで判定する
# パターン2: 4本の線でどの色からどの色へ向けて動いたかを判定する
crossing_system = _safe_int(analysis_config.get("crossing_system"), CONFIG_DEFAULTS["crossing_system"])  # 1 または 2 を選択

# ラインの座標と色（crossing_system = 1 の場合）
line_start = _to_point(analysis_config.get("line_start"), _to_point(CONFIG_DEFAULTS["line_start"], (0, 0)))
line_end = _to_point(analysis_config.get("line_end"), _to_point(CONFIG_DEFAULTS["line_end"], (0, 0)))
line_color = (0, 255, 255)  # 黄色

#  ４つの点の座標（crossing_system = 2 の場合）
p1 = _to_point(analysis_config.get("p1"), _to_point(CONFIG_DEFAULTS["p1"], (0, 0)))
p2 = _to_point(analysis_config.get("p2"), _to_point(CONFIG_DEFAULTS["p2"], (0, 0)))
p3 = _to_point(analysis_config.get("p3"), _to_point(CONFIG_DEFAULTS["p3"], (0, 0)))
p4 = _to_point(analysis_config.get("p4"), _to_point(CONFIG_DEFAULTS["p4"], (0, 0)))

# 除外エリアを定義（多角形の頂点を座標で指定）
exclude_polygon = _to_polygon(analysis_config.get("exclude_polygon"), _to_polygon(CONFIG_DEFAULTS["exclude_polygon"], []))



# YOLOv11のモデル設定（設定ファイル読み込み時に検証済み）

# 検知するクラスを設定
# クラスの対応: 0 - 人, 1 - 自転車, 2 - 車, 3 - オートバイ, 4 - 飛行機, 5 - バス, 6 - 電車, 7 - トラック
target_classes = _to_int_list(analysis_config.get("target_classes"), CONFIG_DEFAULTS["target_classes"])

# YOLOv11の信頼度しきい値設定（上記で読み込んだ値を使用）
# 推奨：0.2～0.5の範囲で調整。
# コツ：しきい値が低いと、検知率は高くなるが誤検知が増える。高くすると誤検知は減るが、検出漏れが発生しやすくなる。
# 目的に応じて、精度と検知率のバランスを考慮して設定を調整。


std_acc = _safe_float(analysis_config.get("std_acc"), CONFIG_DEFAULTS["std_acc"])
x_std_meas = _safe_float(analysis_config.get("x_std_meas"), CONFIG_DEFAULTS["x_std_meas"])
y_std_meas = _safe_float(analysis_config.get("y_std_meas"), CONFIG_DEFAULTS["y_std_meas"])


# 右下の時刻表示のOCR
timestamp_ocr = _safe_bool(analysis_config.get("timestamp_ocr"), CONFIG_DEFAULTS["timestamp_ocr"])  # 動画内に時刻が表示されている場合はTrue、ない場合はFalse

# OCR用のボックス位置（右下に時刻が表示されている領域）
ocr_box = _to_box(analysis_config.get("ocr_box"), _to_box(CONFIG_DEFAULTS["ocr_box"], (0, 0, 0, 0)))  # 時刻が表示されているエリアの左上角(x1, y1)と右下角(x2, y2)の座標を適切に変更してください

# 推定時刻の表示設定
show_estimated_time = _safe_bool(analysis_config.get("show_estimated_time"), CONFIG_DEFAULTS["show_estimated_time"]) # 推定時刻を表示する場合はTrue、表示しない場合はFalse、ASFファイルでは正確な値を返さない可能性があります。

# 開始時刻と終了時刻の設定
start_time_str = _safe_str(
    analysis_config.get("start_time_str"),
    CONFIG_DEFAULTS["start_time_str"],
)  # 開始時刻
end_time_str = _safe_str(
    analysis_config.get("end_time_str"),
    CONFIG_DEFAULTS["end_time_str"],
)    # 終了時刻

# 開始時刻と終了時刻をdatetime形式に変換
try:
    start_time = _parse_datetime_with_optional_date(start_time_str, "start_time_str")
    end_time = _parse_datetime_with_optional_date(end_time_str, "end_time_str")
except ValueError as exc:
    messagebox.showerror("時間形式エラー", str(exc))
    raise SystemExit(1) from exc

if end_time <= start_time:
    messagebox.showerror(
        "時間指定エラー",
        "end_time_str は start_time_str より後の時刻を指定してください。\n"
        "日をまたぐ場合は、年月日を含めて 'YYYY-MM-DD HH:MM:SS' 形式で入力してください。"
    )
    raise SystemExit(1)

start_time_dt = start_time

# プレビュー画面の表示設定
preview = _safe_bool(analysis_config.get("preview"), CONFIG_DEFAULTS["preview"])  # プレビューをオンにする場合はTrue、オフにする場合はFalse

# 出力設定（出力はMP4形式で固定）
fourcc = cv2.VideoWriter_fourcc(*'mp4v')
out = None

# 出力動画のフレームレート（解析したフレームを 1 秒間に 30 枚として出力）
OUTPUT_FPS = 30.0

# 動画フォルダのパス（MP4およびAVIファイルが格納されているフォルダ）
video_dir_value = analysis_config.get("video_dir")
if video_dir_value is None:
    video_dir_value = analysis_config.get("video_folder")
video_dir = _safe_str(video_dir_value, CONFIG_DEFAULTS["video_dir"])
if not os.path.isabs(video_dir):
    video_dir = os.path.normpath(os.path.join(analysis_config_dir, video_dir))
if not os.path.isdir(video_dir):
    messagebox.showerror("動画フォルダエラー", f"動画フォルダが見つかりません: {video_dir}")
    raise SystemExit(1)

output_dir = _safe_str(analysis_config.get("output_dir"), CONFIG_DEFAULTS["output_dir"])  # 出力するディレクトリ
if not os.path.isabs(output_dir):
    output_dir = os.path.normpath(os.path.join(analysis_config_dir, output_dir))

search_subdirs = _safe_bool(
    analysis_config.get("search_subdirs"),
    CONFIG_DEFAULTS.get("search_subdirs", False),
)

# 渋滞指数の算出間隔（フレーム数）
congestion_calculation_interval = _safe_int(
    analysis_config.get("congestion_calculation_interval"),
    CONFIG_DEFAULTS["congestion_calculation_interval"],
)  # 必要に応じて変更


# ================== 解析パラメータ設定セクション終わり ==================

# 渋滞指数の算出間隔を数える変数（フレーム数）
congestion_reset_frame = 0  # [渋滞]

#  lines に座標を設定する（crossing_system = 2 の場合）
lines = {
    'R': {'start': p1, 'end': p2, 'color': (0, 0, 255)},   # Red
    'B': {'start': p2, 'end': p3, 'color': (255, 0, 0)},   # Blue
    'Y': {'start': p3, 'end': p4, 'color': (0, 255, 255)}, # Yellow
    'P': {'start': p4, 'end': p1, 'color': (255, 0, 255)}  # Purple
}

line_name_to_color_name = {
    'R': 'Red',
    'B': 'Blue',
    'Y': 'Yellow',
    'P': 'Purple',
}

import os

# 出力ファイルの設定
os.makedirs(output_dir, exist_ok=True)  # ディレクトリがない場合は作成
file_counter = 1  # ファイル番号カウンター
file_size_limit = 0.95 * 1024 * 1024 * 1024  # 3GB (0.95 * 1024^3 バイト)

# 現在のファイルサイズを追跡
current_file_size = 0

# 初期出力ファイル
timestamp = datetime.now().strftime('%Y%m%d%H%M')
output_file = os.path.join(output_dir, f'{timestamp}output_{file_counter}.mp4')

# フォルダ内の動画ファイルリストを取得
VIDEO_EXTS = ('.mp4', '.avi', '.asf', '.mkv', '.mov', '.mts')

if search_subdirs:
    video_files = []
    for root, dirs, files in os.walk(video_dir):
        for name in files:
            if name.lower().endswith(VIDEO_EXTS):
                rel_path = os.path.relpath(os.path.join(root, name), video_dir)
                video_files.append(rel_path)
else:
    video_files = [
        f
        for f in os.listdir(video_dir)
        if f.lower().endswith(VIDEO_EXTS)
    ]

video_files.sort(key=lambda x: os.path.basename(x).lower())

if not video_files:
    print("No video files found in the specified folder.")
    raise SystemExit(0)

# ※※※※トータルフレーム数の計算と表示 ※※※※
overall_total_frames = 0
file_frame_counts = []

for video_file in video_files:
    video_path = os.path.join(video_dir, video_file)
    cap = cv2.VideoCapture(video_path)

    # フレーム数を取得
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames == 0:
        print(f"Failed to retrieve frames from {video_file}. Skipping this file.")
        cap.release()
        continue

    file_frame_counts.append((video_file, total_frames))
    overall_total_frames += total_frames
    cap.release()

# 全ファイルの総フレーム数を保持
total_frames = overall_total_frames

# 解析するファイルのリストを表示
print("解析するファイル:")
for filename, frames in file_frame_counts:
    print(f"{filename}    frame {frames}")
print(f"total frame {total_frames}")

# sorted_keysを定義（crossing_system = 1 の場合）
sorted_keys = ['person_LtoR', 'bicycle_LtoR', 'car_LtoR', 'motorcycle_LtoR', 
                   'airplane_LtoR', 'bus_LtoR', 'train_LtoR', 'truck_LtoR', 
                   'person_RtoL', 'bicycle_RtoL', 'car_RtoL', 'motorcycle_RtoL', 
                   'airplane_RtoL', 'bus_RtoL', 'train_RtoL', 'truck_RtoL'] 
# カウント用の辞書（crossing_system = 1 の場合）
cross_counts = {key: 0 for key in sorted_keys}

# クラスごとのクロスカウント辞書（crossing_system = 2 の場合）
cross_counts_RBYP = {
    'R_B': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
    'R_Y': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
    'R_P': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
    'B_R': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
    'B_Y': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
    'B_P': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
    'Y_R': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
    'Y_B': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
    'Y_P': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
    'P_R': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
    'P_B': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
    'P_Y': {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0},
}

# クラス名とラインコンボのリストを定義
class_names = ['person', 'bicycle', 'car', 'motorcycle', 'bus', 'train', 'truck', 'airplane']
line_combos = ['R_B', 'R_Y', 'R_P', 'B_R', 'B_Y', 'B_P', 'Y_R', 'Y_B', 'Y_P', 'P_R', 'P_B', 'P_Y']

# ショートネームとクラス名の対応辞書
class_shortnames = {'person': 'pe', 'bicycle': 'bi', 'car': 'ca', 'motorcycle': 'mc', 'airplane': 'ap', 'bus': 'bu', 'train': 'tn', 'truck': 'tk'}



#【関数定義】is_line_crossedは、入力された2点(start,end)がラインとクロスしているかどうかを判定する関数です（crossing_system = 1,2 の場合に使用）
def is_line_crossed(start, end, line_start, line_end):
    def ccw(A, B, C):
        return (C[1] - A[1]) * (B[0] - A[0]) > (B[1] - A[1]) * (C[0] - A[0])
    # 線分上に点があるかを確認する補助関数
    def is_point_on_line(point, line_start, line_end):
        if min(line_start[0], line_end[0]) <= point[0] <= max(line_start[0], line_end[0]) and \
           min(line_start[1], line_end[1]) <= point[1] <= max(line_start[1], line_end[1]):
            # 線分と点が同じ直線上にあるか確認（クロス積がゼロに近いかどうか）
            cross_product = (point[1] - line_start[1]) * (line_end[0] - line_start[0]) - \
                            (point[0] - line_start[0]) * (line_end[1] - line_start[1])
            return abs(cross_product) < 1e-6  # ほぼゼロなら線上
        return False
    # T字型の交差も含めて判定
    if is_point_on_line(start, line_start, line_end) or is_point_on_line(end, line_start, line_end) or \
       is_point_on_line(line_start, start, end) or is_point_on_line(line_end, start, end):
        return True
    # 2つの線分が交差しているかを判定
    return ccw(start, line_start, line_end) != ccw(end, line_start, line_end) and \
           ccw(start, end, line_start) != ccw(start, end, line_end)



#【関数定義】 外積を計算して、ラインに対する移動方向を判定する関数（crossing_system = 1 の場合に使用）
def calculate_cross_product(start, end, line_start, line_end):
    # ラインベクトルを定義
    line_vec = np.array([line_end[0] - line_start[0], line_end[1] - line_start[1]])
    
    # 移動ベクトルを定義（前の位置から現在の位置へのベクトル）
    movement_vec = np.array([end[0] - start[0], end[1] - start[1]])
    
    # 外積を計算（2Dの場合、z成分のみが必要）
    cross_product = line_vec[0] * movement_vec[1] - line_vec[1] * movement_vec[0]
    
    return cross_product



#【関数定義】 OCRテキストから日時を推定する関数
def _parse_ocr_datetime(ocr_text: str):
    """OCR文字列から日付+時刻を抽出してdatetimeを返す。失敗時はNone。"""

    if not ocr_text:
        return None
    s = ocr_text.strip()
    match = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2}).*?(\d{1,2}):(\d{2}):(\d{2})", s)
    if not match:
        return None

    year, month, day, hour, minute, second = map(int, match.groups())
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


#【関数定義】 フレームごとにOCRでテキストを読み取り、表示する関数
def draw_ocr_text(frame):
    # OCRボックス領域を抽出
    ocr_roi = frame[ocr_box[1]:ocr_box[3], ocr_box[0]:ocr_box[2]]
    
    # OCRでテキストを読み取る
    ocr_text = pytesseract.image_to_string(ocr_roi, config='--psm 6')
    
    # 読み取ったテキストを上に表示
    cv2.putText(frame, ocr_text.strip(), (ocr_box[0], ocr_box[1] - 40),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    
    return ocr_text.strip()



#【関数定義】 """ファイルが他のプログラムで開かれている場合、メッセージを表示して閉じるように促す。"""
def wait_for_file_closure(file_path):
    while True:
        try:
            # ファイルを開いてみる（書き込みモード）
            with open(file_path, mode='w', newline='') as file:
                # ファイルが開けた場合、問題なしとしてループを抜ける
                break
        except PermissionError:
            # ファイルが開かれている場合、メッセージボックスを表示
            root = tk.Tk()
            root.withdraw()  # メインウィンドウを非表示にする

            messagebox.showinfo(
                "ファイルが開かれています",
                f"ファイル '{file_path}' が他のプログラムで開かれています。\n"
                "ファイルを閉じてからOKを押してください。"
            )
            root.destroy()



#【関数定義】 ポイントが多角形内にあるかを判定する関数
# この関数は Ray Casting アルゴリズムを使用しており、任意の形状の多角形に対して有効
# ただし、自己交差する多角形には対応していません（必要であれば `shapely` の使用を検討）

def is_point_in_polygon(point, polygon):
    x, y = point
    n = len(polygon)
    inside = False
    p1x, p1y = polygon[0]
    for i in range(n + 1):
        p2x, p2y = polygon[i % n]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y
    return inside



#【関数定義】DeepSORTのトラッキング結果から重複するトラックを除外
def filter_duplicate_tracks(tracking_results_with_class):
    """
    DeepSORTのトラッキング結果から重複するトラックを除外。

    Args:
        tracking_results_with_class (list): トラッキング結果（[{"id": ID, "bbox": (x1, y1, width, height), "class_id": クラスID}, ...]）

    Returns:
        list: 重複を除外したトラッキング結果。
    """
    # グループ化されたクラスID
    vehicle_classes = [2, 5, 7]  # 車、トラック、バス
    bicycle_motorbike_classes = [1, 3]  # 自転車、オートバイ

    filtered_results = []
    used_ids = set()  # 重複排除に使用するIDのセット

    for i, track1 in enumerate(tracking_results_with_class):
        if track1["id"] in used_ids:
            continue  # 既に処理済みのIDはスキップ

        bbox1 = track1["bbox"]
        class_id1 = track1["class_id"]
        confidence1 = track1.get("confidence", 1.0)
        id1 = track1["id"]

        is_duplicate = False

        for j, track2 in enumerate(tracking_results_with_class):
            if i == j or track2["id"] in used_ids:
                continue

            bbox2 = track2["bbox"]
            class_id2 = track2["class_id"]
            confidence2 = track2.get("confidence", 1.0)
            id2 = track2["id"]

            # IoUを計算
            iou = compute_iou(bbox1, bbox2)

            # 同じグループ内の重複を処理
            if iou > iou_threshold:
                if (
                    (class_id1 in vehicle_classes and class_id2 in vehicle_classes) or
                    (class_id1 in bicycle_motorbike_classes and class_id2 in bicycle_motorbike_classes)
                ):
                    # IDが少ない方（古い方）を残す
                    if id1 < id2:  # 古いIDを優先
                        used_ids.add(id2)
                    else:
                        used_ids.add(id1)
                        is_duplicate = True
                        break



        if not is_duplicate:
            filtered_results.append(track1)
            used_ids.add(track1["id"])

    return filtered_results


#【関数定義】  IoU（Intersection over Union）の計算
def compute_iou(box1, box2):
    """
    IoU（Intersection over Union）を計算する。

    Args:
        box1 (list): [x1, y1, width, height]
        box2 (list): [x1, y1, width, height]

    Returns:
        float: IoUスコア
    """
    x1_inter = max(box1[0], box2[0])
    y1_inter = max(box1[1], box2[1])
    x2_inter = min(box1[0] + box1[2], box2[0] + box2[2])
    y2_inter = min(box1[1] + box1[3], box2[1] + box2[3])

    if x1_inter >= x2_inter or y1_inter >= y2_inter:
        return 0.0

    intersection_area = (x2_inter - x1_inter) * (y2_inter - y1_inter)
    box1_area = box1[2] * box1[3]
    box2_area = box2[2] * box2[3]

    return intersection_area / float(box1_area + box2_area - intersection_area)


            
#===============================================================================================
#===============================================================================================
#==================                        解析                               ==================
#===============================================================================================
#===============================================================================================

# 解析の開始時に開始時間を表示
start_time_analysis = datetime.now()  # 解析の実際の開始時間を取得
print(f"解析開始時間: {start_time_analysis.strftime('%Y-%m-%d %H:%M:%S')}")

# 車両のトラッキングに使う辞書
tracked_vehicles = {} # 現在追跡中の車両リスト、すでに検出され追跡している車両のIDがキーとして、
                      #{"kalman_filter": kf, "age": 0, "position": (centerX, centerY, w, h, class_id)}が値として格納されます
vehicle_ages = {}  # 車両ごとの観測されなかったフレーム数を保持する辞書
cross_texts = {}  # 車両IDごとの通過テキストを保持する辞書
temporary_detections = {}  # 一時的な検出を追跡する辞書を定義
first_cross = {}  # 各車両が最初に横切ったラインを保持する辞書
cross_colors = {}  # 各車両のIDごとにテキストの色を記録する辞書 例: cross_colors[vehicle_id] = (255, 0, 0)  # 赤色

frame_inverse_distances = []  # 各フレームの累積逆距離を格納[渋滞]
frame_time_stamps = []  # フレームのタイムスタンプまたはインデックスを格納[渋滞]
current_congestion_index = "---"  # 渋滞指数の初期値[渋滞]
frame_cumulative_inverse_distance = 0  # このフレームの累積逆距離を初期化[渋滞]
previous_positions = {}  # {vehicle_id: (prev_centerX, prev_centerY)} 過去の位置を保持する辞書
track_first_seen_frame: dict[int, int] = {}

# グループ化されたクラスID
vehicle_classes = [2, 5, 7]  # 車、トラック、バスのクラスIDリスト
bicycle_motorbike_classes = [1, 3]  # 自転車、オートバイのクラスIDリスト
unified_classes = vehicle_classes + bicycle_motorbike_classes  # 統一されたクラスIDリスト


# 車両ID用のカウンタ
vehicle_id_counter = 0

# CSVファイルの準備（Crossing / Congestion 共通ベースタイムスタンプ）
base_timestamp = datetime.now().strftime('%Y%m%d%H%M')

# 2列目のタイトルを timestamp_ocr に応じて変更
if timestamp_ocr:
    time_column_title = 'OCR Time (HH:MM:SS)'
else:
    time_column_title = 'Estimated Time (YYYY-MM-DD HH:MM:SS)'

csv_columns = ['Frame', time_column_title, 'Direction', 'StartColor', 'EndColor', 'ClassName', 'LifetimeSec']

# crossing_log 日付分割用の管理変数
_crossing_file_counter = 0
_crossing_writers: dict[str, csv.writer] = {}
_crossing_files: dict[str, object] = {}


def _get_crossing_writer_for_time(time_value: str) -> csv.writer:
    """
    time_value から日付キーを取り出し、その日付用の crossing_log-*.csv writer を返す。
    初めてのキーであればファイルを新規作成する。
    """

    global _crossing_file_counter

    date_key = _extract_date_key_from_timestr(time_value)
    if date_key not in _crossing_writers:
        _crossing_file_counter += 1
        csv_file_path = os.path.join(
            output_dir,
            f"{base_timestamp}crossing_log-{_crossing_file_counter}.csv",
        )
        # 他プロセスで開かれていないかチェック
        wait_for_file_closure(csv_file_path)

        f = open(csv_file_path, mode='w', newline='', encoding='utf-8')
        w = csv.writer(f)
        w.writerow(csv_columns)  # ヘッダー
        _crossing_files[date_key] = f
        _crossing_writers[date_key] = w
        print(f"[AICount] crossing_log opened: {csv_file_path} (date_key={date_key})")

    return _crossing_writers[date_key]


# ここから解析を始めます
processed_frames = 0  #---------------------------------------------------------------ここから解析を始めます
last_progress_percent = 0

for index, video_file in enumerate(video_files, start=1):  #------------------------------動画ファイルの数だけ繰り返します
    video_path = os.path.join(video_dir, video_file)  # 対象のファイルパスを取得
    cap = cv2.VideoCapture(video_path)  # cv2.VideoCapture は、OpenCV というライブラリで使用されるクラスで、動画やカメラからフレームを取得するために使用されます。

    if not cap.isOpened():  # ファイルが開けなかった場合のエラーチェック
        print(f"Error: Unable to open video file {video_file}. Skipping...")
        continue

    video_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))  # 動画の幅を取得
    video_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))  # 動画の高さを取得
    input_fps = cap.get(cv2.CAP_PROP_FPS)  # 動画のフレームレートを取得（解析用）
    if not input_fps or input_fps != input_fps:
        input_fps = 29.97
        print(f"[INFO] FPS fallback applied (29.97) for: {video_path}")

    if video_width == 0 or video_height == 0 or input_fps == 0:  # 正常に取得できなかった場合のチェック
        print(f"Error: Invalid video dimensions or FPS in file {video_file}. Skipping...")
        cap.release()
        continue

    if out is None:  # 初めてのファイルの場合のみビデオ出力の初期化を行います
        # 出力ファイルの初期化
        out = cv2.VideoWriter(output_file, fourcc, OUTPUT_FPS, (video_width, video_height))
        print(f"Initialized video writer with dimensions ({video_width}x{video_height}) and FPS {OUTPUT_FPS}.")

    print(f"[start] {video_file} ({index}/{len(video_files)})")
    file_processed_frames = 0  #---------------------------------------------------------------動画ファイルごとのフレーム数

    # スクリプト内の処理を繰り返す
    while True:
        ret, frame = cap.read()  # ビデオからフレームをキャプチャーします。1フレーム目から順番に読み取ります。
        if not ret:              # もう、この動画からフレームが無ければwhile構文はbreakされます（次の動画へ）
            break
            
        # 指定フレームごとに解析を実行（ファイルの最初はスキップしない）
        if file_processed_frames % frame_skip != 0 and file_processed_frames != 0: 
            file_processed_frames += 1  # 個別ファイルのフレームカウンターを1増やして
            processed_frames += 1       # 全体のフレームカウンターを1増やして
            continue  # 次のフレームへ

        #===============================================================================================
        #====　フレーム毎の解析　第1章　初期設定　======================================================
        #===============================================================================================

        #このフレームで使う辞書、リスト、セットを初期化
        current_frame_cross_check = False  # 現在のフレームで交差が発生したかどうかのチェック変数を初期化
        current_frame_cross_counts = {key: 0 for key in cross_counts.keys()}  # フレームごとの交差回数をリセット(crossing_system == 1)
        current_frame_cross_counts_RBYP = {combo: {cls: 0 for cls in class_names} for combo in line_combos} # フレームごとの交差回数をリセット(crossing_system == 2)
        frame_cross_events = []  # フレーム内で交差があった車両の情報を格納するリスト
        # === YOLOv11による推論 ===
        # RTX 5080 (SM120) + 古い Ultralytics では FP16 カーネルが非対応のため、
        # half=True / model.half() を使うと
        # "no kernel image is available for execution on the device"
        # でクラッシュする。
        # そのため、ここでは明示的に FP32 (half=False) を強制する。
        predict_kwargs = {"device": yolo_device}
        if getattr(_cli_args, "skip_model_to", False):
            dev = yolo_device
            if isinstance(dev, str) and dev.startswith("cuda"):
                dev = "0"
            predict_kwargs["device"] = dev
            log(f"[CHK] predict device override: {predict_kwargs['device']}")
        with torch.inference_mode():
            results_list = model.predict(
                source=frame,
                half=False,
                verbose=False,
                **predict_kwargs,
            )
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            results = results_list[0]

        # YOLOの推論結果のループ
        deepsort_detections = []  # DeepSORT用の検出結果リスト
        hungarian_detections = []  # ハンガリー法用の検出結果リスト
        bytetrack_dets = []

        if getattr(results, "boxes", None) is not None:
            xyxy = results.boxes.xyxy
            confs = results.boxes.conf
            clses = results.boxes.cls

            if hasattr(xyxy, "cpu"):
                xyxy = xyxy.cpu().numpy()
                confs = confs.cpu().numpy()
                clses = clses.cpu().numpy().astype(int)
            else:
                xyxy = np.asarray(xyxy)
                confs = np.asarray(confs)
                clses = np.asarray(clses, dtype=int)

            for (x1, y1, x2, y2), conf, class_id in zip(xyxy, confs, clses):
                class_id_int = int(class_id)
                if conf < confidence_threshold or class_id_int not in target_classes:
                    continue
                width = x2 - x1
                height = y2 - y1
                bbox_tlwh = [float(x1), float(y1), float(width), float(height)]

                centerX = x1 + width / 2
                centerY = y1 + height * 0.9
                if is_point_in_polygon((centerX, centerY), exclude_polygon):
                    continue

                deepsort_detections.append((bbox_tlwh, float(conf), class_id_int))
                hungarian_detections.append(
                    {
                        "bbox": (float(x1), float(y1), float(x2), float(y2)),
                        "center": (int(centerX), int(centerY)),
                        "class_id": class_id_int,
                        "confidence": float(conf),
                        "width": float(width),
                        "height": float(height),
                    }
                )
                bytetrack_dets.append([float(x1), float(y1), float(x2), float(y2), float(conf), float(class_id_int)])

        tracking_results_with_class = []

        if tracking_method == "bytetrack":
            dets_np = np.asarray(bytetrack_dets, dtype=np.float32) if bytetrack_dets else np.empty((0, 6), dtype=np.float32)
            debug_log("[CHK] bytetrack.update start")
            tracks = tracker.update(dets_np, frame)
            debug_log("[CHK] bytetrack.update done")
            for row in tracks:
                if row is None or len(row) < 7:
                    continue

                x1, y1, x2, y2, track_id, score, cls_id = map(float, row[:7])

                left = x1
                top = y1
                width = max(1.0, x2 - x1)
                height = max(1.0, y2 - y1)

                tracking_results_with_class.append(
                    {
                        "id": int(track_id),
                        "bbox": [left, top, width, height],
                        "class_id": int(cls_id),
                    }
                )
            tracking_results_with_class.sort(key=lambda x: x["id"])
            tracking_results_with_class = filter_duplicate_tracks(tracking_results_with_class)

            for track in tracking_results_with_class:
                track_id = track["id"]
                left, top, width, height = track["bbox"]
                class_id = track["class_id"]
                if track_id not in track_first_seen_frame:
                    track_first_seen_frame[track_id] = processed_frames
                color = (0, 255, 0)

                cv2.rectangle(frame, (int(left), int(top)), (int(left + width), int(top + height)), color, 2)
                class_name = model_names.get(int(class_id), "Unknown") if class_id is not None else "Unknown"
                cv2.putText(frame, f'ID: {track_id}', (int(left), int(top) - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
                cv2.putText(frame, class_name, (int(left), int(top) - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        elif tracking_method == "deepsort":
            try:
                tracking_results = tracker.update_tracks(deepsort_detections, frame=frame)
            except Exception as e:
                print("Error in DeepSORT update_tracks:", e)
                raise

            for track in tracking_results:
                if not track.is_confirmed():
                    continue

                track_id = track.track_id
                left, top, width, height = track.to_tlwh()
                class_id = getattr(track, "det_class", None)

                tracking_results_with_class.append(
                    {
                        "id": track_id,
                        "bbox": [left, top, width, height],
                        "class_id": class_id,
                    }
                )
                if track_id not in track_first_seen_frame:
                    track_first_seen_frame[track_id] = processed_frames

            tracking_results_with_class.sort(key=lambda x: x["id"])
            tracking_results_with_class = filter_duplicate_tracks(tracking_results_with_class)

            for track in tracking_results_with_class:
                track_id = track["id"]
                left, top, width, height = track["bbox"]
                class_id = track["class_id"]
                color = (0, 255, 0)

                cv2.rectangle(frame, (int(left), int(top)), (int(left + width), int(top + height)), color, 2)
                class_name = model_names.get(int(class_id), "Unknown") if class_id is not None else "Unknown"
                cv2.putText(frame, f'ID: {track_id}', (int(left), int(top) - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
                cv2.putText(frame, class_name, (int(left), int(top) - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        else:
            # ハンガリーアルゴリズムを用いたトラッキング処理
            filtered_detections = []
            for i, det in enumerate(hungarian_detections):
                keep = True
                for j, other in enumerate(hungarian_detections):
                    if i == j:
                        continue
                    iou = compute_iou_xyxy(det["bbox"], other["bbox"])
                    if iou > iou_threshold and (
                        det["class_id"] == other["class_id"]
                        or (
                            det["class_id"] in vehicle_classes
                            and other["class_id"] in vehicle_classes
                        )
                        or (
                            det["class_id"] in bicycle_motorbike_classes
                            and other["class_id"] in bicycle_motorbike_classes
                        )
                    ):
                        keep = False
                        break
                if keep:
                    filtered_detections.append(det)

            hungarian_detections = filtered_detections

            detection_centers = [
                {
                    "center": det["center"],
                    "bbox": det["bbox"],
                    "class_id": det["class_id"],
                    "confidence": det["confidence"],
                }
                for det in hungarian_detections
            ]

            assigned_ids = assign_ids_with_hungarian(
                detection_centers,
                tracked_vehicles,
                distance_threshold,
                confidence_threshold,
            )

            detected_ids = set()
            for i, detection in enumerate(hungarian_detections):
                x1, y1, x2, y2 = detection["bbox"]
                w = max(1, int(round(x2 - x1)))
                h = max(1, int(round(y2 - y1)))
                centerX, centerY = detection["center"]
                class_id = detection["class_id"]

                assigned_id = assigned_ids.get(i)
                if assigned_id is None:
                    assigned_id = vehicle_id_counter
                    vehicle_id_counter += 1
                    tracked_vehicles[assigned_id] = {
                        "position": (centerX, centerY, w, h, class_id),
                        "yolo_position": (centerX, centerY),
                        "prev_position": None,
                        "last_yolo_position": None,
                        "kalman_filter": KalmanFilter(
                            dt, centerX, centerY, std_acc, x_std_meas, y_std_meas
                        ),
                        "age": 0,
                    }
                    first_cross[assigned_id] = None
                    if assigned_id not in track_first_seen_frame:
                        track_first_seen_frame[assigned_id] = processed_frames
                else:
                    if assigned_id not in tracked_vehicles:
                        tracked_vehicles[assigned_id] = {
                            "position": (centerX, centerY, w, h, class_id),
                            "yolo_position": (centerX, centerY),
                            "prev_position": None,
                            "last_yolo_position": None,
                            "kalman_filter": KalmanFilter(
                                dt, centerX, centerY, std_acc, x_std_meas, y_std_meas
                            ),
                            "age": 0,
                        }
                        first_cross[assigned_id] = None
                        track_first_seen_frame[assigned_id] = processed_frames
                    else:
                        prev_position = tracked_vehicles[assigned_id].get("position")
                        if prev_position is not None:
                            tracked_vehicles[assigned_id]["prev_position"] = prev_position[:2]
                        tracked_vehicles[assigned_id]["last_yolo_position"] = tracked_vehicles[assigned_id].get(
                            "yolo_position"
                        )
                        tracked_vehicles[assigned_id]["kalman_filter"].update(
                            np.array([[centerX], [centerY]])
                        )
                        tracked_vehicles[assigned_id]["position"] = (
                            centerX,
                            centerY,
                            w,
                            h,
                            class_id,
                        )
                        tracked_vehicles[assigned_id]["yolo_position"] = (centerX, centerY)
                        tracked_vehicles[assigned_id]["age"] = 0

                if assigned_id not in track_first_seen_frame:
                    track_first_seen_frame[assigned_id] = processed_frames
                detected_ids.add(assigned_id)

                cv2.rectangle(
                    frame,
                    (int(x1), int(y1)),
                    (int(x2), int(y2)),
                    (0, 255, 0),
                    2,
                )
                class_name = model_names.get(class_id, "Unknown")
                cv2.putText(
                    frame,
                    f'ID: {assigned_id}',
                    (int(x1), int(y1) - 15),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 255, 0),
                    2,
                )
                cv2.putText(
                    frame,
                    class_name,
                    (int(x1), int(y1) - 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 255, 0),
                    2,
                )

            for vehicle_id in list(tracked_vehicles.keys()):
                if vehicle_id not in detected_ids:
                    tracked_vehicles[vehicle_id]["age"] += 1
                    if tracked_vehicles[vehicle_id]["age"] > max_age_config:
                        tracked_vehicles.pop(vehicle_id, None)
                        cross_texts.pop(vehicle_id, None)
                        first_cross.pop(vehicle_id, None)
                        cross_colors.pop(vehicle_id, None)
                        previous_positions.pop(vehicle_id, None)
                        track_first_seen_frame.pop(vehicle_id, None)
                        continue

                    predicted_center = tracked_vehicles[vehicle_id]["kalman_filter"].predict().A1
                    predicted_centerX, predicted_centerY = int(predicted_center[0]), int(predicted_center[1])

                    prev_data = tracked_vehicles[vehicle_id].get("position")
                    if prev_data is not None:
                        tracked_vehicles[vehicle_id]["prev_position"] = prev_data[:2]
                        w, h, class_id = prev_data[2], prev_data[3], prev_data[4]
                        tracked_vehicles[vehicle_id]["position"] = (
                            predicted_centerX,
                            predicted_centerY,
                            w,
                            h,
                            class_id,
                        )

                        cv2.circle(frame, (predicted_centerX, predicted_centerY), 5, (180, 180, 180), -1)
                        prev_centerX, prev_centerY = int(prev_data[0]), int(prev_data[1])
                        cv2.line(
                            frame,
                            (prev_centerX, prev_centerY),
                            (predicted_centerX, predicted_centerY),
                            (180, 180, 180),
                            2,
                        )
                        class_name = model_names.get(class_id, "Unknown")
                        cv2.putText(
                            frame,
                            f'ID: {vehicle_id}',
                            (predicted_centerX, predicted_centerY - 15),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.5,
                            (180, 180, 180),
                            2,
                        )
                        cv2.putText(
                            frame,
                            class_name,
                            (predicted_centerX, predicted_centerY - 30),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.5,
                            (180, 180, 180),
                            2,
                        )

            for vehicle_id, vehicle_data in tracked_vehicles.items():
                position = vehicle_data.get("position")
                if not position:
                    continue
                centerX, centerY, w, h, class_id = position
                left = centerX - w / 2
                top = centerY - h * 0.9
                tracking_results_with_class.append(
                    {
                        "id": vehicle_id,
                        "bbox": [left, top, w, h],
                        "class_id": class_id,
                    }
                )

            tracking_results_with_class.sort(key=lambda x: x["id"])
        #===============================================================================================
        #====  第4章  フレーム毎の解析（ハンガリー方式ではカルマン予測を実施）  ====================
        #===============================================================================================


        #===============================================================================================
        #====  第5章　クロス判定　                                                      ================
        #===============================================================================================
        

        # Deep SORTのトラッキング結果を使ってクロス判定
        for vehicle_data in tracking_results_with_class:  # 追跡中の車両ごとにループ処理を実行

            # トラッキング結果から情報を取得
            vehicle_id = vehicle_data["id"]
            left, top, width, height = vehicle_data["bbox"]  # DeepSORT形式

            centerX = int(left + width / 2)  # 横方向の中央
            centerY = int(top + height * 0.9)  # 縦方向は下から10%の位置
            class_id = vehicle_data["class_id"]

            # 過去の位置情報は直接Deep SORTの補完結果から算出されるため、特別な処理は不要
            prev_centerX, prev_centerY = previous_positions.get(vehicle_id, (centerX, centerY))  # 1フレーム前の位置を取得（存在しない場合は現在の位置を使用）

            # 渋滞指数の計算
            distance = np.sqrt((centerX - prev_centerX) ** 2 + (centerY - prev_centerY) ** 2)
            frame_cumulative_inverse_distance += 1 / (1 + (distance / frame.shape[1]) * 500)  # 累積に加算

            # 現在の位置を次フレーム用に保存
            previous_positions[vehicle_id] = (centerX, centerY)


            #=================================================================================================================
            # パターン1: 1本の線でLtoRとRtoLで判定する (crossing_system = 1)
            #=================================================================================================================
        
            if crossing_system == 1:

                # ベクトル外積を使ってクロス方向を判定
                cross_product = calculate_cross_product((prev_centerX, prev_centerY), (centerX, centerY), line_start, line_end)
                if is_line_crossed((prev_centerX, prev_centerY), (centerX, centerY), line_start, line_end):
                    class_name = model_names.get(class_id, "Unknown") # クラスIDからクラス名（例: 車、自転車など）を取得
                    direction = 'LtoR' if cross_product > 0 else 'RtoL'  # 外積が正ならLtoR、負ならRtoL
                    cross_key = f"{class_name}_{direction}"  # クラス名と方向を組み合わせて一意のキーを作成
                
                    if cross_key in cross_counts:  # このクラス名と方向のキーがcross_countsに存在するか確認
                        cross_counts[cross_key] += 1  # このクラスと方向のカウントを1増やす
                        current_frame_cross_counts[cross_key] += 1  # 現フレームのカウントも1増やす
                        cross_texts[vehicle_id] = f"{class_name} {direction.replace('to', ' to ')}"  # 車両IDに対して、交差した方向のテキストを登録
                        current_frame_cross_check = True  # 交差があったことを記録
                        frame_cross_events.append(
                            {
                                "vehicle_id": vehicle_id,
                                "direction": direction,
                                "start_color": _get_color_name(line_color, "N/A"),
                                "end_color": _get_color_name(line_color, "N/A"),
                                "class_name": class_name,
                            }
                        )




            #=================================================================================================================
            # パターン2: 4本の線でどの色からどの色へ向けて動いたかを判定する (crossing_system = 2)
            #=================================================================================================================

            elif crossing_system == 2:

                # 現在の車両がどの線を跨いだかを判定
                crossed_line_name = None
                for line_name, line_coords in lines.items():
                    if is_line_crossed((prev_centerX, prev_centerY), (centerX, centerY), line_coords['start'], line_coords['end']):
                        crossed_line_name = line_name
                        break  # 最初に跨いだ線が見つかったらループを抜ける

                # 跨いだ線がある場合
                if crossed_line_name:
                    if first_cross.get(vehicle_id) is None:
                        # 最初に跨いだ線を登録
                        first_cross[vehicle_id] = crossed_line_name
                        cross_texts[vehicle_id] = f"{crossed_line_name}" # 車両IDに対して、最初に交差したラインの名まえを登録
                        cross_colors[vehicle_id] = lines[crossed_line_name]['color']  # 最初に交差したラインの色を記録
                    elif crossed_line_name != first_cross[vehicle_id]: #最初の色と今回の色が同じだったらパス
                        start_line = first_cross[vehicle_id] # 最初に跨いだ線
                        end_line = crossed_line_name # 現在跨いだ線
                        line_combo = f"{start_line}_{end_line}" # line_combo = 'R_B' のようにラインの組み合わせを作成
                        cross_texts[vehicle_id] = f"{line_combo}"  # 車両IDに対して、最初に交差したラインの名まえを登録
                        current_frame_cross_check = True # 交差があったことを記録
                        # 該当するラインコンボのカウントを増加
                        class_name = model_names.get(class_id, "Unknown")  # クラス名を取得
                        if class_name in class_shortnames:
                            short_name = class_shortnames[class_name]
                            if line_combo in cross_counts_RBYP:
                                cross_counts_RBYP[line_combo][short_name] += 1
                                current_frame_cross_counts_RBYP[line_combo][class_name] += 1  # カウントを増やす
                                frame_cross_events.append(
                                    {
                                        "vehicle_id": vehicle_id,
                                        "direction": line_combo,
                                        "start_color": line_name_to_color_name.get(start_line, start_line),
                                        "end_color": line_name_to_color_name.get(end_line, end_line),
                                        "class_name": class_name,
                                    }
                                )

                        # first_cross をリセット
                            first_cross[vehicle_id] = None


            #=================================================================================================================

            # バウンディングボックス内にテキストを表示
            if vehicle_id in cross_texts: # 車両IDがcross_textsに含まれている場合、表示
                text = cross_texts[vehicle_id] # テキストサイズを取得
                (text_width, text_height), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.0, 2)
                text_color = cross_colors.get(vehicle_id, (0, 255, 0)) # テキストの色を保持するためにcross_colorsから取得(該当する色がなければ緑を使用)
                cv2.putText(frame, cross_texts[vehicle_id], (centerX - (text_width // 2), centerY + (text_height // 2)),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.6, text_color, 2)


        # ライン交差があった場合のみログに記録
        if current_frame_cross_check and frame_cross_events:
            # 推定時刻をログに使うかOCR結果を使うかを判断
            lifetime_per_frame_sec = None
            if timestamp_ocr:
                ocr_text = draw_ocr_text(frame)  # フレームにOCRで読み取った時間を表示し、テキストを取得
                dt = _parse_ocr_datetime(ocr_text)
                time_value = _format_timestamp_with_seconds(dt, ocr_text)
            elif show_estimated_time:
                # 推定時刻の計算
                total_duration = (end_time - start_time).total_seconds()  # 総時間を計算
                estimated_time = start_time_dt + timedelta(
                    seconds=(total_duration * processed_frames / total_frames)
                )  # 現在の推定時刻

                time_value = _format_timestamp_with_seconds(estimated_time, "N/A")
                if total_frames > 0:
                    lifetime_per_frame_sec = total_duration / float(total_frames)
            else:
                time_value = "N/A"  # どちらも表示しない場合は "N/A" とする

            writer = _get_crossing_writer_for_time(time_value)
            seen_keys = set()
            for event in frame_cross_events:
                vid = event.get("vehicle_id")
                direction = event.get("direction", "")
                start_color = event.get("start_color", "N/A")
                end_color = event.get("end_color", "N/A")
                class_name = event.get("class_name", "")
                key = (vid, direction, start_color, end_color, class_name)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                first_frame = track_first_seen_frame.get(vid)
                if lifetime_per_frame_sec is None or first_frame is None:
                    lifetime_sec = 0
                else:
                    frame_delta = max(processed_frames - first_frame, 0)
                    lifetime_sec = int(round(frame_delta * lifetime_per_frame_sec))

                row = [
                    processed_frames,
                    time_value,
                    direction,
                    start_color,
                    end_color,
                    class_name,
                    lifetime_sec,
                ]
                writer.writerow(row)

        # congestion_calculation_intervalフレームごとに渋滞指数を記録[渋滞]
        congestion_reset_frame += 1
        if congestion_reset_frame >= congestion_calculation_interval:    # 渋滞指数算出間隔は冒頭で定義[渋滞]
            time_value = "N/A"
            if timestamp_ocr:
                ocr_text = draw_ocr_text(frame)  # フレームにOCRで読み取った時間を表示し、テキストを取得
                dt = _parse_ocr_datetime(ocr_text)
                time_value = _format_timestamp_with_seconds(dt, ocr_text)
            elif show_estimated_time:  # 推定時刻の計算
                total_duration = (end_time - start_time).total_seconds() # 総時間を計算
                estimated_time = start_time_dt + timedelta(
                    seconds=(total_duration * processed_frames / total_frames)
                ) # 現在の推定時刻
                time_value = _format_timestamp_with_seconds(estimated_time, "N/A")
                    
            # 渋滞指数とタイムスタンプを保存[渋滞]
            frame_inverse_distances.append(round(frame_cumulative_inverse_distance / congestion_calculation_interval, 3))  # 渋滞指数を四捨五入して整数化[渋滞]
            frame_time_stamps.append(time_value)  # OCRまたは推定時刻を記録[渋滞]
            frame_cumulative_inverse_distance = 0  # このフレームの累積逆距離を初期化[渋滞]
            congestion_reset_frame = 0 # カウンターリセット[渋滞]
            current_congestion_index = round(frame_inverse_distances[-1], 3)  # 最新の渋滞指数を取得し、小数点2桁に丸める
            
            

        #===============================================================================================
        #====　フレーム毎の処理　第6章　フレームに情報表示　============================================
        #===============================================================================================


        # --- RoadLabo credit + runtime device overlay (top-left) ---
        credit1 = "This software is provided by RoadLabo."
        credit2 = "Free to use"

        runtime_lines = _runtime_device_texts()
        device_line = runtime_lines[0] if runtime_lines else ""
        yolo_label = os.path.basename(yolo_weights)

        header_parts = [device_line, yolo_label, credit1, credit2]
        header_text = " | ".join(part for part in header_parts if part)

        font_scale = 0.55
        thickness_outline = 3
        thickness_text = 1

        (text_width, text_height), _ = cv2.getTextSize(
            header_text,
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            thickness_text,
        )
        max_width = frame.shape[1] - 20

        while text_width > max_width and font_scale > 0.3:
            font_scale -= 0.05
            (text_width, text_height), _ = cv2.getTextSize(
                header_text,
                cv2.FONT_HERSHEY_SIMPLEX,
                font_scale,
                thickness_text,
            )

        org = (10, 24)
        cv2.putText(
            frame,
            header_text,
            org,
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (0, 0, 0),
            thickness_outline,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            header_text,
            org,
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (255, 255, 255),
            thickness_text,
            cv2.LINE_AA,
        )

        y_offset = 70
        
        # フレームに渋滞指数を表示
        congestion_text = f"Traffic Density {current_congestion_index}"  # 表示用のテキストを作成
        (text_width, text_height), baseline = cv2.getTextSize(congestion_text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        # テキスト背景を描画
        cv2.rectangle(frame, (10, y_offset - 5),
                      (10 + text_width, y_offset + text_height + baseline), (0, 0, 0), -1)
        # テキストを描画
        cv2.putText(frame, congestion_text, (10, y_offset + text_height),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        y_offset += text_height + 20  # 表示位置を次に進める

        # フレームに除外エリアを描画(オレンジ色ラインで多角形を描画します)
        for i in range(len(exclude_polygon)):
            start_point = exclude_polygon[i]
            end_point = exclude_polygon[(i + 1) % len(exclude_polygon)]  # 次の頂点（最後の頂点の場合は最初の頂点に戻る）
            cv2.line(frame, start_point, end_point, (255, 165, 0), 2)

        if crossing_system == 1:   # crossing_system = 1 の場合
        
            # フレームにラインを描画(毎フレームに描画します)
            cv2.line(frame, line_start, line_end, line_color, 2)

            # L to R ，R to L　タイトルを表示
            ltor_text = "L->R , L<-R"
            (ltor_text_width, ltor_text_height), ltor_baseline = cv2.getTextSize(ltor_text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            cv2.rectangle(frame, (10, y_offset - 5),
                          (10 + ltor_text_width, y_offset + ltor_text_height + ltor_baseline), (0, 0, 0), -1)
            cv2.putText(frame, ltor_text, (10, y_offset + ltor_text_height), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            y_offset += ltor_text_height + 18  # タイトルの後にスペースを追加

                
                
            # L to R と R to L のカウントをまとめて表示
            for class_name in [v for k, v in model_names.items() if k in target_classes]:
                ltor_key = f"{class_name}_LtoR"
                rtol_key = f"{class_name}_RtoL"
                ltor_count = cross_counts.get(ltor_key, 0)
                rtol_count = cross_counts.get(rtol_key, 0)

                # クラス名と L to R / R to L カウントを表示
                text = f"{class_name}:  {ltor_count},  {rtol_count}"
                (text_width, text_height), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
                cv2.rectangle(frame, (10, y_offset - 5),
                              (10 + text_width, y_offset + text_height + baseline), (0, 0, 0), -1)
                cv2.putText(frame, text, (10, y_offset + text_height),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
                y_offset += text_height + 13



       
        elif crossing_system == 2:   # crossing_system = 2 の場合
            
            # フレームに4本のラインを描画(毎フレームに描画します)
            for line_name, line in lines.items():
                cv2.line(frame, line['start'], line['end'], line['color'], 2)
        
            # タイトル行を表示 (target_classesに含まれるクラスのみ)
            # target_classes に対応するクラス名を取得
            selected_class_names = [v for k, v in model_names.items() if k in target_classes]
            header_text = ', '.join(selected_class_names)

            # ヘッダーテキストの描画
            (header_text_width, header_text_height), header_baseline = cv2.getTextSize(header_text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            cv2.rectangle(frame, (10, y_offset - 5),
                          (10 + header_text_width, y_offset + header_text_height + header_baseline), (0, 0, 0), -1)
            cv2.putText(frame, header_text, (10, y_offset + header_text_height), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            y_offset += header_text_height + 18  # タイトルの後にスペースを追加

            # 各ラインの組み合わせごとのカウントを表示
            line_combinations = ['R_B', 'R_Y', 'R_P', 'B_R', 'B_Y', 'B_P', 'Y_R', 'Y_B', 'Y_P', 'P_R', 'P_B', 'P_Y']
            line_number = 0
            for line_combo in line_combinations:
                # 各クラスごとのカウントを取得
                line_combo_counts = cross_counts_RBYP.get(line_combo, {'pe': 0, 'bi': 0, 'ca': 0, 'mc': 0, 'ap': 0, 'bu': 0, 'tn': 0, 'tk': 0})

                # 表示テキストの作成
                counts_text = f"{line_combo}"
                for class_name, short_name in class_shortnames.items():
                    # target_classesに含まれるクラスのみ表示
                    class_id = next((k for k, v in model_names.items() if v == class_name), None)
                    if class_id in target_classes:
                        counts_text += f", {line_combo_counts[short_name]}"

                # テキストの描画（counts_textに表示するものがある場合のみ描画）
                if counts_text != f"{line_combo}: ":
                    (counts_text_width, counts_text_height), counts_baseline = cv2.getTextSize(counts_text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
                    cv2.rectangle(frame, (10, y_offset - 2),
                                  (10 + counts_text_width, y_offset + counts_text_height + counts_baseline), (0, 0, 0), -1)
                    cv2.putText(frame, counts_text, (10, y_offset + counts_text_height), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)

                line_number += 1
                if line_number % 3 == 0 :
                    y_offset += counts_text_height + 20  # 行ごとに20ピクセル多くy_offsetを増やす（間隔を広くする）
                else: 
                    y_offset += counts_text_height + 13  # それ以外は通常の13ピクセル分y_offsetを増やす（標準の行間隔）




        # crossing_system=1と2の共通事項


        # フレーム数表示の前に1行分のスペースを追加
        y_offset += 10

        # フレーム数を表示
        if total_frames > 0:
            progress = (processed_frames / total_frames) * 100
        else:
            progress = 0
        frame_text = f'  Frame: {processed_frames}/{total_frames} ({progress:.1f}%)'
        (frame_text_width, frame_text_height), frame_baseline = cv2.getTextSize(frame_text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(frame, (10, y_offset - 5),
                      (10 + frame_text_width, y_offset + frame_text_height + frame_baseline), (0, 0, 0), -1)
        cv2.putText(
            frame,
            frame_text,
            (10, y_offset + frame_text_height),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
        )
        
        # 推定時刻を表示するかどうかの設定
        if show_estimated_time:
            
            y_offset += 30

            # フレームレートから推定終了時刻と現在の推定時刻を計算
            total_duration = (end_time - start_time).total_seconds()  # 総時間
            current_time = start_time_dt + timedelta(seconds=(total_duration * processed_frames / total_frames))  # 現在の推定時刻

            # 推定時刻表示
            time_text = f'  Time: {current_time.strftime(TIMESTAMP_FORMAT)}'
            (time_text_width, time_text_height), time_baseline = cv2.getTextSize(time_text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            cv2.rectangle(frame, (10, y_offset - 5),
                          (10 + time_text_width, y_offset + time_text_height + time_baseline), (0, 0, 0), -1)
            cv2.putText(frame, time_text, (10, y_offset + time_text_height), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)            

        # フレームを書き込み
        out.write(frame)      # output.mp4の最後にこの解析結果フレームを1枚追加
        
        # ファイルが存在する場合のみサイズを取得(v06追加事項)
        if os.path.exists(output_file):
            current_file_size = os.path.getsize(output_file)
        else:
            current_file_size = 0

        # ファイルサイズが900MBを超えた場合、新しいファイルに切り替える
        if current_file_size > file_size_limit:
            # 現在の出力ファイルを閉じる
            out.release()

            # 新しいファイルに切り替え
            file_counter += 1
            output_file = os.path.join(output_dir, f'{timestamp}output_{file_counter}.mp4')
            # 常に 30fps 固定で新しいファイルを開始
            out = cv2.VideoWriter(output_file, fourcc, OUTPUT_FPS, (video_width, video_height))
            current_file_size = 0  # サイズカウンタをリセット
        
        #v06追加ここまで
        
        # プレビュー表示（必要に応じて）
        if preview:
            safe_imshow('Frame', frame)

        # フレーム数カウント
        file_processed_frames += 1  # 個別ファイルのフレームカウンターを1増やして
        processed_frames += 1       # 全体のフレームカウンターを1増やして
            
        # 進捗率を計算
        if total_frames > 0:
            progress_percent = (processed_frames / total_frames) * 100
        else:
            progress_percent = 0

        # 進捗が10%以上進んだ場合に表示
        if progress_percent >= last_progress_percent + 10:
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f'Processing: {round(progress_percent)}% complete at {current_time}')
            last_progress_percent += 10  # 次の10%進捗に進む
            
        
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break


        # ================================================ 次のフレームへ ==================================================

    cap.release()   
    # ================================================ 次の動画ファイルへ ===============================================

# 渋滞指数 CSV の日付分割用管理
_congestion_file_counter = 0
_congestion_writers: dict[str, csv.writer] = {}
_congestion_files: dict[str, object] = {}


def _get_congestion_writer_for_time(time_value: str) -> csv.writer:
    """
    time_value から日付キーを取り、その日付用の congestion_index-*.csv writer を返す。
    初めてのキーであればファイルを新規作成する。
    """

    global _congestion_file_counter

    date_key = _extract_date_key_from_timestr(time_value)
    if date_key not in _congestion_writers:
        _congestion_file_counter += 1
        csv_file_path = os.path.join(
            output_dir,
            f"{base_timestamp}congestion_index-{_congestion_file_counter}.csv",
        )
        wait_for_file_closure(csv_file_path)

        f = open(csv_file_path, mode='w', newline='', encoding='utf-8')
        w = csv.writer(f)
        w.writerow(["Time", "Traffic Density"])
        _congestion_files[date_key] = f
        _congestion_writers[date_key] = w
        print(f"[AICount] congestion_index opened: {csv_file_path} (date_key={date_key})")

    return _congestion_writers[date_key]


# すべてのフレームを処理した後に渋滞指数をCSVに出力
for time_index, congestion_index in zip(frame_time_stamps, frame_inverse_distances):
    writer = _get_congestion_writer_for_time(time_index)
    writer.writerow([time_index, congestion_index])  # OCRまたは推定時刻を使用

# congestion_index の全ファイルをクローズ
for f in _congestion_files.values():
    try:
        f.close()
    except Exception:
        pass

print(f"渋滞データを {_congestion_file_counter} 個の CSV に保存しました")


# 終了処理
# スクリプトの終了時に out が初期化されているかを確認してから release() を呼び出す
if out is not None:
    out.release()
    print("Output video saved successfully.")
else:
    print("No video was processed, output not saved.")
    print("Hint: check that '.mts' is included in the extension filter and OpenCV has FFmpeg support.")

cv2.destroyAllWindows()

# crossing_log の全ファイルをクローズ
for f in _crossing_files.values():
    try:
        f.close()
    except Exception:
        pass

# カウント結果を表示
for key in sorted_keys:
    value = cross_counts[key]
    print(f'{key.replace("_", " ").capitalize()}: {value}')
    
# 解析の終了時に所要時間を表示
end_time_analysis = datetime.now()  # 解析の終了時間を取得
elapsed_time = end_time_analysis - start_time_analysis  # 所要時間を計算
print(f"解析開始時間: {start_time_analysis.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"解析終了時間: {end_time_analysis.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"所要時間: {elapsed_time}")

# 解析ログファイルの出力
try:
    log_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    log_filename = f"{log_timestamp}LOG.txt"
    log_path = os.path.join(output_dir, log_filename)

    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"file_count: {len(video_files)}\n")
        log.write(f"analysis_start: {start_time_analysis.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log.write(f"analysis_end:   {end_time_analysis.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log.write(f"elapsed:        {elapsed_time}\n")

        log.write("\n[env]\n")
        env_keys = [
            "runtime_device",
            "gpu_name",
            "cpu_fallback",
            "yolo_device",
            "yolo_model",
            "tracking_method",
            "torch_version",
            "cuda_version",
            "ultralytics_version",
            "boxmot_version",
            "opencv_version",
            "numpy_version",
            "python_version",
            "os",
        ]
        for key in env_keys:
            value = env_info.get(key, "") if isinstance(env_info, dict) else ""
            log.write(f"{key}: {value}\n")

        log.write("\n[config]\n")
        for key in sorted(analysis_config.keys()):
            value = analysis_config[key]
            log.write(f"{key}: {value}\n")

        log.write("\n[files]\n")
        for vf in video_files:
            log.write(f"{vf}\n")

    print(f"解析ログを出力しました: {log_path}")
except Exception as exc:
    print(f"解析ログの出力に失敗しました: {exc}", file=sys.stderr)
