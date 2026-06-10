"""Local-side command processor."""
from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

from utils.atomic_file import atomic_write_json, read_json


class CommandProcessor:
    def __init__(self, commands_dir: str | Path, handler, logger: logging.Logger, interval: int = 3) -> None:
        self.commands_dir = Path(commands_dir)
        self.pending_dir = self.commands_dir / "pending"
        self.processed_dir = self.commands_dir / "processed"
        self.failed_dir = self.commands_dir / "failed"
        self.handler = handler
        self.logger = logger.getChild("commands")
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        for path in (self.pending_dir, self.processed_dir, self.failed_dir):
            path.mkdir(parents=True, exist_ok=True)
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="CommandProcessor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def process_once(self) -> None:
        for path in sorted(self.pending_dir.glob("*.json")):
            try:
                command = read_json(path)
                result = self.handler(command)
                self._move_with_result(path, self.processed_dir, {"ok": True, "result": result})
                self.logger.info("commands処理: %s", command.get("type"))
            except Exception as exc:
                self._move_with_result(path, self.failed_dir, {"ok": False, "error": str(exc)})
                self.logger.exception("commands処理失敗: %s", path)

    def _move_with_result(self, path: Path, target_dir: Path, result: dict) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        result_path = target_dir / f"{path.stem}.result.json"
        atomic_write_json(result_path, result)
        shutil.move(str(path), str(target_dir / path.name))

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.process_once()
            self._stop.wait(self.interval)
