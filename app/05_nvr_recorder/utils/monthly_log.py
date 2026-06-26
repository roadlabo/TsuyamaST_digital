"""Monthly log writer for the NVR tools."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


class MonthlyLogHandler(logging.Handler):
    def __init__(self, log_dir: str | Path, prefix: str = "nvr", max_index: int = 20) -> None:
        super().__init__()
        self.log_dir = Path(log_dir)
        self.prefix = prefix
        self.max_index = max_index
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record) + "\n"
            for path in self.paths_for_now():
                try:
                    with path.open("a", encoding="utf-8") as f:
                        f.write(line)
                    return
                except OSError:
                    continue
            self.handleError(record)
        except Exception:
            self.handleError(record)

    def paths_for_now(self) -> list[Path]:
        ym = datetime.now().strftime("%Y-%m")
        paths = [self.log_dir / f"{self.prefix}_{ym}.log"]
        for index in range(2, self.max_index + 1):
            paths.append(self.log_dir / f"{self.prefix}_{ym}-{index}.log")
        return paths


def latest_log_file(log_dir: str | Path, prefix: str = "nvr") -> Path | None:
    root = Path(log_dir)
    if not root.exists():
        return None
    files = [p for p in root.glob(f"{prefix}_*.log") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def read_latest_log_text(log_dir: str | Path, limit_chars: int = 8000, prefix: str = "nvr") -> str:
    path = latest_log_file(log_dir, prefix)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit_chars:]
    except OSError as exc:
        return f"Log read error: {path}\n{exc}"
