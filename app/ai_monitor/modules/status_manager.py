from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


class StatusManager:
    def __init__(self, ai_status_path: Path):
        self.ai_status_path = ai_status_path
        self.last_level = None

    def decide_level(
        self,
        cam1_over: bool,
        cam2_long_stay_count: int,
        cam2_long_stay_trigger_count: int,
        cam3_over: bool,
    ) -> str:
        if cam3_over:
            return "LEVEL3"
        if cam1_over:
            return "LEVEL1"
        if cam2_long_stay_count >= cam2_long_stay_trigger_count:
            return "LEVEL2"
        return "LEVEL0"

    def update_if_needed(self, level: str) -> None:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = {"congestion_level": level, "updated_at": now_str}
        if self.last_level == level and self.ai_status_path.exists():
            return
        self._atomic_write_json(self.ai_status_path, payload)
        self.last_level = level

    @staticmethod
    def _atomic_write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
