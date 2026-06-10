"""Periodic status JSON writer owned by the local PC."""
from __future__ import annotations

import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from utils.atomic_file import atomic_write_json


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class StatusWriter:
    def __init__(self, settings: dict, camera_status_provider: Callable[[], list[dict]], system_status_provider: Callable[[], str]) -> None:
        self.settings = settings
        self.camera_status_provider = camera_status_provider
        self.system_status_provider = system_status_provider
        self.status_dir = Path(settings["status_dir"])
        self.interval = int(settings.get("status_interval_seconds", 5))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="StatusWriter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self.write_once()

    def write_once(self) -> None:
        self.status_dir.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.settings["archive_dir"])
        payload = {
            "updated_at": now_text(),
            "system": {
                "status": self.system_status_provider(),
                "disk_free_gb": round(usage.free / (1024 ** 3), 1),
                "archive_dir": self.settings["archive_dir"],
                "temp_dir": self.settings["temp_dir"],
            },
            "cameras": self.camera_status_provider(),
        }
        atomic_write_json(self.status_dir / "system_status.json", payload)
        atomic_write_json(self.status_dir / "cameras_status.json", {"updated_at": payload["updated_at"], "cameras": payload["cameras"]})

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.write_once()
            self._stop.wait(self.interval)
