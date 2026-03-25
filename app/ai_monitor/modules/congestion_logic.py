from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class CongestionState:
    movement_history: deque[float] = field(default_factory=lambda: deque(maxlen=30))
    last_center_by_track: dict[int, tuple[float, float]] = field(default_factory=dict)


class CongestionScorer:
    """AICount11.py の "台数 + 動き" 発想を 0-100 に整理した初版スコア。"""

    def __init__(self) -> None:
        self.state = CongestionState()

    def update(self, tracks: list[dict]) -> float:
        if not tracks:
            self.state.movement_history.append(0.0)
            return 0.0

        active_count = len(tracks)
        movement = 0.0
        moved_count = 0

        for tr in tracks:
            tid = int(tr["track_id"])
            cx, cy = tr["center"]
            prev = self.state.last_center_by_track.get(tid)
            if prev is not None:
                dx = cx - prev[0]
                dy = cy - prev[1]
                movement += (dx * dx + dy * dy) ** 0.5
                moved_count += 1
            self.state.last_center_by_track[tid] = (cx, cy)

        avg_motion = movement / max(1, moved_count)
        self.state.movement_history.append(avg_motion)
        baseline_motion = sum(self.state.movement_history) / max(1, len(self.state.movement_history))

        density_component = min(100.0, active_count * 10.0)
        stagnation = 1.0 - min(1.0, avg_motion / max(1.0, baseline_motion + 1e-6))
        stagnation_component = max(0.0, min(100.0, stagnation * 100.0))

        score = 0.65 * density_component + 0.35 * stagnation_component
        return max(0.0, min(100.0, score))
