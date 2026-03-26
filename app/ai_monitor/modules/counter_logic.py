from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CounterState:
    counted_track_ids: set[int] = field(default_factory=set)
    previous_side: dict[int, float] = field(default_factory=dict)
    pass_bins_ltor: list[int] = field(default_factory=lambda: [0] * 144)
    pass_bins_rtol: list[int] = field(default_factory=lambda: [0] * 144)


class LineCounter:
    def __init__(self, line_points: list[list[int]]):
        self.line_points = line_points
        self.state = CounterState()

    def update_line(self, line_points: list[list[int]]) -> None:
        self.line_points = line_points
        self.state.previous_side.clear()
        self.state.counted_track_ids.clear()

    def _signed_side(self, center: tuple[float, float]) -> float:
        p1, p2 = self.line_points
        vx, vy = p2[0] - p1[0], p2[1] - p1[1]
        wx, wy = center[0] - p1[0], center[1] - p1[1]
        return vx * wy - vy * wx

    def update(self, track_id: int, center: tuple[float, float], class_name: str, now: datetime):
        if len(self.line_points) != 2:
            return None

        current_side = self._signed_side(center)
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
