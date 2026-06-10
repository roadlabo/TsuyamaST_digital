"""Retention and free-space cleanup utilities."""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from config.config_store import CameraConfig


def quarantine_old_partials(temp_dir: str | Path, logger: logging.Logger) -> None:
    temp = Path(temp_dir)
    if not temp.exists():
        return
    quarantine = temp / "_quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    for partial in temp.glob("**/*.partial"):
        if quarantine in partial.parents:
            continue
        target = quarantine / f"{datetime.now():%Y%m%d_%H%M%S}_{partial.name}"
        shutil.move(str(partial), str(target))
        logger.info("起動時partial隔離: %s -> %s", partial, target)


def cleanup_by_retention(archive_dir: str | Path, cameras: list[CameraConfig], logger: logging.Logger) -> None:
    archive = Path(archive_dir)
    now = datetime.now()
    for camera in cameras:
        cutoff = now - timedelta(days=camera.retention_days)
        for mp4 in (archive / camera.save_subdir).glob("**/*.mp4"):
            try:
                mtime = datetime.fromtimestamp(mp4.stat().st_mtime)
                if mtime < cutoff:
                    mp4.unlink()
                    logger.info("古いファイル削除(retention): %s", mp4)
            except FileNotFoundError:
                continue


def cleanup_for_free_space(archive_dir: str | Path, min_free_gb: float, logger: logging.Logger) -> None:
    archive = Path(archive_dir)
    archive.mkdir(parents=True, exist_ok=True)
    def free_gb() -> float:
        return shutil.disk_usage(archive).free / (1024 ** 3)
    if free_gb() >= min_free_gb:
        return
    files = sorted(archive.glob("**/*.mp4"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    for mp4 in files:
        if free_gb() >= min_free_gb:
            return
        try:
            mp4.unlink()
            logger.warning("容量不足のため古い順に削除: %s", mp4)
        except FileNotFoundError:
            pass
