import json
import logging
import os
import subprocess
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

CONFIG_PATH = Path("C:/_TsuyamaSignage/app/config/config.json")
ACTIVE_PATH = Path("C:/_TsuyamaSignage/app/config/active.json")
DEFAULT_LOG_DIR = Path("C:/_TsuyamaSignage/logs")
PLAYLIST_DIR = Path("C:/_TsuyamaSignage/app/config")

RETRY_MISSING_SECONDS = 30
RETRY_PLAYER_SECONDS = 10

# 監視（ポーリング）間隔：短いほど反応は良いが負荷が少し増える
WATCH_POLL_SECONDS = 2.0

# active.json / config.json の変更検知は mtime で行う（Windowsでも軽い）
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


def safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


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


def start_player(cmd: List[str]) -> subprocess.Popen:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(cmd, creationflags=creationflags)


def stop_player(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


@dataclass(frozen=True)
class FolderState:
    # ファイル名 -> (サイズ, mtime) で差し替え/更新も検知
    items: Tuple[Tuple[str, int, float], ...]

    @staticmethod
    def from_folder(folder: Path) -> "FolderState":
        files = list_mp4_files(folder)
        items: List[Tuple[str, int, float]] = []
        for p in files:
            try:
                st = p.stat()
                items.append((p.name, int(st.st_size), float(st.st_mtime)))
            except FileNotFoundError:
                # 列挙中に消された等は無視（次の監視周期で整合する）
                continue
        return FolderState(items=tuple(sorted(items)))


def run_player_with_watch(
    *,
    active_channel: str,
    content_root: Path,
    fullscreen: bool,
    active_mtime_at_start: float,
    config_mtime_at_start: float,
) -> int:
    """
    再生プロセスを起動し、以下を監視して変更があれば終了（=上位ループで再起動）:
      - active.json の更新（チャンネル切替）
      - config.json の更新（設定変更）
      - チャンネルフォルダ内 mp4 の増減・差し替え
    """
    channel_folder = content_root / active_channel
    if not channel_folder.exists():
        logger.error("Active channel folder not found: %s", channel_folder)
        return 3

    files = list_mp4_files(channel_folder)
    if not files:
        logger.error("No mp4 files found in %s", channel_folder)
        return 4

    # 初期プレイリスト
    playlist_path = build_playlist(active_channel, files)
    logger.info("Active channel: %s", active_channel)
    sample = ", ".join([f.name for f in files[:5]])
    logger.info("Playlist items (first 5): %s", sample)

    cmd = find_player_command(playlist_path, fullscreen)
    logger.info("Launching player: %s", " ".join(cmd))

    proc = start_player(cmd)
    last_folder_state = FolderState.from_folder(channel_folder)

    try:
        while True:
            # プロセスが落ちたら終了コードで戻す（上位で再起動）
            exit_code = proc.poll()
            if exit_code is not None:
                return int(exit_code)

            # 監視
            time.sleep(WATCH_POLL_SECONDS)

            # active.json が変わったらチャンネル切替の可能性 → 再起動
            active_mtime_now = safe_mtime(ACTIVE_PATH)
            if active_mtime_now != active_mtime_at_start:
                logger.info("Detected active.json change. Restarting player to apply.")
                stop_player(proc)
                return 0

            # config.json が変わったら設定変更の可能性 → 再起動
            config_mtime_now = safe_mtime(CONFIG_PATH)
            if config_mtime_now != config_mtime_at_start:
                logger.info("Detected config.json change. Restarting player to apply.")
                stop_player(proc)
                return 0

            # チャンネルフォルダ内が変わったらプレイリストを作り直して再起動
            if not channel_folder.exists():
                logger.info("Channel folder disappeared. Restarting.")
                stop_player(proc)
                return 0

            current_state = FolderState.from_folder(channel_folder)
            if current_state != last_folder_state:
                logger.info("Detected folder content change. Rebuilding playlist and restarting.")
                files_now = list_mp4_files(channel_folder)
                if not files_now:
                    logger.error("No mp4 files after change in %s", channel_folder)
                    stop_player(proc)
                    return 0
                build_playlist(active_channel, files_now)
                stop_player(proc)
                return 0
    finally:
        stop_player(proc)


def main() -> None:
    configure_logging(DEFAULT_LOG_DIR)
    logger.info("auto_play.py starting")

    while True:
        try:
            config_mtime = safe_mtime(CONFIG_PATH)
            active_mtime = safe_mtime(ACTIVE_PATH)

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

            # 再生 + 監視（変更検知で戻ってくる）
            exit_code = run_player_with_watch(
                active_channel=active_channel,
                content_root=content_root,
                fullscreen=fullscreen,
                active_mtime_at_start=active_mtime,
                config_mtime_at_start=config_mtime,
            )

            # プレイヤーが自然終了した場合も再起動
            logger.error(
                "Player loop ended (code=%s). Restarting in %s seconds.",
                exit_code,
                RETRY_PLAYER_SECONDS,
            )
            time.sleep(RETRY_PLAYER_SECONDS)

        except Exception:
            logger.exception("Unhandled error. Retrying in %s seconds.", RETRY_MISSING_SECONDS)
            time.sleep(RETRY_MISSING_SECONDS)


if __name__ == "__main__":
    main()
