import json
import logging
import os
import subprocess
import shutil
import time
from pathlib import Path
from typing import List

CONFIG_PATH = Path("C:/_TsuyamaSignage/app/config/config.json")
ACTIVE_PATH = Path("C:/_TsuyamaSignage/app/config/active.json")
DEFAULT_LOG_DIR = Path("C:/_TsuyamaSignage/app/logs")
PLAYLIST_DIR = Path("C:/_TsuyamaSignage/app/config")

RETRY_MISSING_SECONDS = 30
RETRY_PLAYER_SECONDS = 10

logger = logging.getLogger("auto_play")


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "auto_play.log"
    if logger.handlers:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.propagate = False


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def list_mp4_files(folder: Path) -> List[Path]:
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"]
    return sorted(files, key=lambda p: p.name)


def build_playlist(channel: str, files: List[Path]) -> Path:
    playlist_path = PLAYLIST_DIR / f"playlist_{channel}.m3u"
    lines = [str(path) for path in files]
    playlist_path.write_text("\n".join(lines), encoding="utf-8")
    return playlist_path


def find_player_command(playlist_path: Path, fullscreen: bool) -> List[str]:
    mpv_path = shutil.which("mpv")
    if mpv_path:
        cmd = [
            mpv_path,
            "--no-terminal",
            "--no-osd-bar",
            "--osd-level=0",
            "--loop-playlist=inf",
        ]
        if fullscreen:
            cmd.append("--fullscreen")
        cmd.append(str(playlist_path))
        return cmd

    vlc_path = shutil.which("vlc") or shutil.which("vlc.exe")
    if vlc_path:
        cmd = [
            vlc_path,
            "--no-video-title-show",
            "--loop",
        ]
        if fullscreen:
            cmd.append("--fullscreen")
        cmd.append(str(playlist_path))
        return cmd

    raise FileNotFoundError("Neither mpv nor VLC was found on PATH.")


def wait_for_player(cmd: List[str]) -> int:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(cmd, creationflags=creationflags)
    return process.wait()


def main() -> None:
    configure_logging(DEFAULT_LOG_DIR)
    logger.info("auto_play.py starting")

    while True:
        try:
            config = load_json(CONFIG_PATH)
            log_dir = Path(config.get("log_dir", str(DEFAULT_LOG_DIR)))
            if log_dir != DEFAULT_LOG_DIR:
                configure_logging(log_dir)
            active = load_json(ACTIVE_PATH)

            active_channel = active.get("active_channel")
            if not active_channel:
                raise ValueError("active_channel is missing or empty.")

            content_root = Path(config["content_root"])
            fullscreen = bool(config.get("fullscreen", True))

            channel_folder = content_root / active_channel
            if not channel_folder.exists():
                logger.error("Active channel folder not found: %s", channel_folder)
                time.sleep(RETRY_MISSING_SECONDS)
                continue

            files = list_mp4_files(channel_folder)
            if not files:
                logger.error("No mp4 files found in %s", channel_folder)
                time.sleep(RETRY_MISSING_SECONDS)
                continue

            playlist_path = build_playlist(active_channel, files)
            logger.info("Active channel: %s", active_channel)
            sample = ", ".join([f.name for f in files[:5]])
            logger.info("Playlist items (first 5): %s", sample)

            cmd = find_player_command(playlist_path, fullscreen)
            logger.info("Launching player: %s", " ".join(cmd))

            exit_code = wait_for_player(cmd)
            logger.error("Player exited with code %s. Restarting in %s seconds.", exit_code, RETRY_PLAYER_SECONDS)
            time.sleep(RETRY_PLAYER_SECONDS)
        except Exception:
            logger.exception("Unhandled error. Retrying in %s seconds.", RETRY_MISSING_SECONDS)
            time.sleep(RETRY_MISSING_SECONDS)


if __name__ == "__main__":
    main()
