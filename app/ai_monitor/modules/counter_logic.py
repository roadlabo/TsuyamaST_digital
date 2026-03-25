from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CounterState:
    counted_track_ids: set[int] = field(default_factory=set)
    previous_center: dict[int, tuple[float, float]] = field(default_factory=dict)
    histogram_10min: list[int] = field(default_factory=lambda: [0] * 144)


class LineCounter:
    def __init__(self, direction: str, line_points: list[list[int]]):
        self.direction = direction
        self.line_points = line_points
        self.state = CounterState()

    def _line_x(self) -> float:
        p1, p2 = self.line_points
        return (p1[0] + p2[0]) / 2.0

    def update(self, track_id: int, center: tuple[float, float], class_name: str, now: datetime):
        prev = self.state.previous_center.get(track_id)
        self.state.previous_center[track_id] = center

        if prev is None or track_id in self.state.counted_track_ids:
            return None

        line_x = self._line_x()
        crossed = False
        if self.direction == "LtoR":
            crossed = prev[0] < line_x <= center[0]
        else:
            crossed = prev[0] > line_x >= center[0]

        if not crossed:
            return None

        self.state.counted_track_ids.add(track_id)
        bin_index = (now.hour * 60 + now.minute) // 10
        if 0 <= bin_index < 144:
            self.state.histogram_10min[bin_index] += 1

        return {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "track_id": track_id,
            "class_name": class_name,
            "direction": self.direction,
        }
