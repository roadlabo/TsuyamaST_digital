"""Office-side command request writer."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from utils.atomic_file import atomic_write_json


class CommandWriter:
    def __init__(self, commands_dir: str | Path, source: str = "office_pc") -> None:
        self.pending_dir = Path(commands_dir) / "pending"
        self.source = source

    def write_command(self, command_type: str, params: dict | None = None) -> Path:
        now = datetime.now()
        command_id = f"{now:%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
        payload = {
            "command_id": command_id,
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "source": self.source,
            "type": command_type,
            "params": params or {},
        }
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        path = self.pending_dir / f"{command_id}.json"
        atomic_write_json(path, payload)
        return path
