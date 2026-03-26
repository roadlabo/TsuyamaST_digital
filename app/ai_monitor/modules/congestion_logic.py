from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class CongestionState:
    frame_inverse_distances: list[float] = field(default_factory=list)
    frame_time_stamps: list[datetime] = field(default_factory=list)
    frame_cumulative_inverse_distance: float = 0.0
    current_congestion_index: float = 0.0
    window_start: datetime | None = None


class CongestionScorer:
    """AICount11.py の congestion 算出式を監視向けに時間窓化して適用。"""

    def __init__(self, interval_sec: int = 10, day_keep: int = 1):
        self.interval_sec = max(1, int(interval_sec))
        self.day_keep = max(1, day_keep)
        self.state = CongestionState()

    def update_interval(self, interval_sec: int) -> None:
        self.interval_sec = max(1, int(interval_sec))

    def _compute_frame_inverse_distance(self, tracks: list[dict], frame_width: int) -> float:
        if len(tracks) < 2 or frame_width <= 0:
            return 0.0

        total = 0.0
        for i in range(len(tracks)):
            x1, y1 = tracks[i]["center"]
            for j in range(i + 1, len(tracks)):
                x2, y2 = tracks[j]["center"]
                distance = ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5
                total += 1 / (1 + (distance / frame_width) * 500)
        return total

    def update(self, tracks: list[dict], now: datetime, frame_width: int) -> float:
        if self.state.window_start is None:
            self.state.window_start = now

        self.state.frame_cumulative_inverse_distance += self._compute_frame_inverse_distance(tracks, frame_width)
        elapsed = (now - self.state.window_start).total_seconds()
        if elapsed < self.interval_sec:
            return self.state.current_congestion_index

        value = round(self.state.frame_cumulative_inverse_distance / self.interval_sec, 3)
        self.state.frame_inverse_distances.append(value)
        self.state.frame_time_stamps.append(now)
        self.state.current_congestion_index = value
        self.state.frame_cumulative_inverse_distance = 0.0
        self.state.window_start = now

        day_ago = now - timedelta(days=self.day_keep)
        while self.state.frame_time_stamps and self.state.frame_time_stamps[0] < day_ago:
            self.state.frame_time_stamps.pop(0)
            self.state.frame_inverse_distances.pop(0)
        return value
