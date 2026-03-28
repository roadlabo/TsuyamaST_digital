from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Iterable, Mapping

LEVEL_STYLE_MAP: dict[int, dict[str, str]] = {
    1: {"label": "渋滞LEVEL1", "bg": "#7fd0ff", "fg": "#000000", "icon": "🟢"},
    2: {"label": "渋滞LEVEL2", "bg": "#ffd166", "fg": "#000000", "icon": "🟡"},
    3: {"label": "渋滞LEVEL3", "bg": "#ff9f1c", "fg": "#000000", "icon": "🟠"},
    4: {"label": "渋滞LEVEL4", "bg": "#e53935", "fg": "#ffffff", "icon": "🔴"},
}


class CongestionSmoother:
    """Simple moving-average smoother for congestion index."""

    def __init__(self, window_size: int = 6):
        self.window_size = max(1, int(window_size))
        self.values: deque[float] = deque(maxlen=self.window_size)

    def update_window_size(self, window_size: int) -> None:
        new_size = max(1, int(window_size))
        if new_size == self.window_size:
            return
        old = list(self.values)
        self.window_size = new_size
        self.values = deque(old[-new_size:], maxlen=new_size)

    def add(self, value: float) -> float:
        self.values.append(float(value))
        return self.current()

    def current(self) -> float:
        if not self.values:
            return 0.0
        return float(sum(self.values) / len(self.values))


def congestion_level_from_index(index_value: float, th2: float, th3: float, th4: float) -> int:
    value = float(index_value)
    if value < float(th2):
        return 1
    if value < float(th3):
        return 2
    if value < float(th4):
        return 3
    return 4


def normalize_congestion_level(value) -> int:
    if isinstance(value, bool):
        return 1
    if isinstance(value, (int, float)):
        try:
            return max(1, min(4, int(value)))
        except Exception:
            return 1

    s = str(value).strip().upper()
    mapping = {
        "LEVEL0": 1,
        "LEVEL1": 1,
        "LEVEL2": 2,
        "LEVEL3": 3,
        "LEVEL4": 4,
        "1": 1,
        "2": 2,
        "3": 3,
        "4": 4,
    }
    return mapping.get(s, 1)


def get_level_thresholds(config: Mapping[str, object] | None = None) -> tuple[float, float, float]:
    cfg = config or {}
    th2 = float(cfg.get("level2_threshold", 8.0))
    th3 = float(cfg.get("level3_threshold", 12.0))
    th4 = float(cfg.get("level4_threshold", 16.0))
    if th2 >= th3:
        th3 = th2 + 0.1
    if th3 >= th4:
        th4 = th3 + 0.1
    return th2, th3, th4


def compute_level_from_status(ai_status: Mapping[str, object], default_thresholds: tuple[float, float, float] = (8.0, 12.0, 16.0)) -> int:
    """Shared resolution logic for controller/monitor.

    Prefer smoothed index + thresholds when present, fallback to stored level.
    """
    if "smoothed_congestion_index" in ai_status:
        th2 = float(ai_status.get("level2_threshold", default_thresholds[0]))
        th3 = float(ai_status.get("level3_threshold", default_thresholds[1]))
        th4 = float(ai_status.get("level4_threshold", default_thresholds[2]))
        return congestion_level_from_index(float(ai_status.get("smoothed_congestion_index", 0.0)), th2, th3, th4)
    return normalize_congestion_level(ai_status.get("congestion_level", 1))


def level_style(level: int) -> dict[str, str]:
    return LEVEL_STYLE_MAP.get(normalize_congestion_level(level), LEVEL_STYLE_MAP[1])


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
