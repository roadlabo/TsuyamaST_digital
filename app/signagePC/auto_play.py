import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"
CONFIG_DIR = APP_DIR / "config"
CONTENT_DIR = ROOT / "content"
LOGS_DIR = ROOT / "logs"
RUNTIME_DIR = ROOT / "runtime"
STATUS_DIR = LOGS_DIR / "status"

PYTHON_EXE = RUNTIME_DIR / "python" / "python.exe"
MPV_EXE = RUNTIME_DIR / "mpv" / "mpv.exe"

DEFAULT_LOG_DIR = LOGS_DIR
HEARTBEAT_PATH = STATUS_DIR / "auto_play_heartbeat.json"
HEARTBEAT_INTERVAL_SEC = 5.0

RETRY_MISSING_SECONDS = 30
RETRY_PLAYER_SECONDS = 10

BLACKOUT_BMP = CONFIG_DIR / "blackout.bmp"
_blackout_proc: subprocess.Popen | None = None

# 監視（ポーリング）間隔：短いほど反応は良いが負荷が少し増える
WATCH_POLL_SECONDS = 2.0

# active.json / config.json の変更検知は mtime で行う（Windowsでも軽い）
logger = logging.getLogger("auto_play")
JST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(JST).isoformat()


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    tmp.replace(path)


@dataclass
class HeartbeatState:
    playlist_dir: str = ""
    current_file: str | None = None
    index: int | None = None
    loop_count: int = 0
    last_change_at: str | None = None

    def set_current(self, filename: str | None, index: int | None) -> None:
        if filename is None:
            return
        if filename != self.current_file:
            self.last_change_at = now_iso()
            if self.index is not None and index is not None and index < self.index:
                self.loop_count += 1
            self.current_file = filename
            self.index = index

    def to_payload(self, *, error: str | None, mpv_pid: int | None) -> dict:
        return {
            "timestamp": now_iso(),
            "pid": os.getpid(),
            "playlist_dir": self.playlist_dir,
            "current_file": self.current_file,
            "index": self.index,
            "loop_count": self.loop_count,
            "last_change_at": self.last_change_at,
            "mpv_pid": mpv_pid,
            "error": error,
        }


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


def load_json(path: Path, fallback: dict | None = None) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return fallback or {}


def safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def list_mp4_files(folder: Path) -> List[Path]:
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"]
    return sorted(files, key=lambda p: p.name)


def build_playlist(playlist_dir: Path, channel: str, files: List[Path]) -> Path:
    playlist_path = playlist_dir / f"playlist_{channel}.m3u"
    lines = [str(path) for path in files]
    playlist_path.write_text("\n".join(lines), encoding="utf-8")
    return playlist_path


def ensure_blackout_bmp(path: Path) -> None:
    if path.exists():
        return
    bmp_bytes = (
        b"BM"
        b"\x3a\x00\x00\x00"
        b"\x00\x00"
        b"\x00\x00"
        b"\x36\x00\x00\x00"
        b"\x28\x00\x00\x00"
        b"\x01\x00\x00\x00"
        b"\x01\x00\x00\x00"
        b"\x01\x00"
        b"\x18\x00"
        b"\x00\x00\x00\x00"
        b"\x04\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x00\x00\x00\x00"
        b"\x00\x00\x00\x00"
    )
    path.write_bytes(bmp_bytes)


def find_player_command(playlist_path: Path, fullscreen: bool) -> List[str]:
    if not MPV_EXE.is_file():
        raise FileNotFoundError(f"mpv.exe not found: {MPV_EXE}")
    cmd = [
        str(MPV_EXE),
        "--no-terminal",
        "--no-osd-bar",
        "--osd-level=0",
        "--loop-playlist=inf",
    ]
    if fullscreen:
        cmd.append("--fullscreen")
    cmd.append(str(playlist_path))
    return cmd


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


def start_blackout(fullscreen: bool) -> subprocess.Popen:
    ensure_blackout_bmp(BLACKOUT_BMP)
    cmd = [
        str(MPV_EXE),
        "--no-terminal",
        "--no-osd-bar",
        "--osd-level=0",
        "--loop-file=inf",
        "--image-display-duration=inf",
    ]
    if fullscreen:
        cmd.append("--fullscreen")
    cmd.append(str(BLACKOUT_BMP))
    return start_player(cmd)


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
    playlist_dir: Path,
    active_path: Path,
    config_path: Path,
    heartbeat: HeartbeatState,
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

    heartbeat.playlist_dir = str(channel_folder)
    heartbeat.set_current(files[0].name, 0)

    # 初期プレイリスト
    playlist_path = build_playlist(playlist_dir, active_channel, files)
    logger.info("Active channel: %s", active_channel)
    sample = ", ".join([f.name for f in files[:5]])
    logger.info("Playlist items (first 5): %s", sample)

    cmd = find_player_command(playlist_path, fullscreen)
    logger.info("Launching player: %s", " ".join(cmd))

    global _blackout_proc
    proc = start_player(cmd)
    if _blackout_proc and _blackout_proc.poll() is None:
        time.sleep(1.0)
        stop_player(_blackout_proc)
        _blackout_proc = None
    last_folder_state = FolderState.from_folder(channel_folder)
    last_heartbeat = time.monotonic()
    write_json_atomic(HEARTBEAT_PATH, heartbeat.to_payload(error=None, mpv_pid=proc.pid))

    try:
        while True:
            # プロセスが落ちたら終了コードで戻す（上位で再起動）
            exit_code = proc.poll()
            if exit_code is not None:
                write_json_atomic(
                    HEARTBEAT_PATH,
                    heartbeat.to_payload(error=f"mpv exited ({exit_code})", mpv_pid=None),
                )
                return int(exit_code)

            # 監視
            time.sleep(WATCH_POLL_SECONDS)

            now_monotonic = time.monotonic()
            if now_monotonic - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                write_json_atomic(HEARTBEAT_PATH, heartbeat.to_payload(error=None, mpv_pid=proc.pid))
                last_heartbeat = now_monotonic

            # active.json が変わったらチャンネル切替の可能性 → 再起動
            active_mtime_now = safe_mtime(active_path)
            if active_mtime_now != active_mtime_at_start:
                logger.info("Detected active.json change. Restarting player to apply.")
                if not _blackout_proc or _blackout_proc.poll() is not None:
                    _blackout_proc = start_blackout(fullscreen)
                stop_player(proc)
                return 0

            # config.json が変わったら設定変更の可能性 → 再起動
            config_mtime_now = safe_mtime(config_path)
            if config_mtime_now != config_mtime_at_start:
                logger.info("Detected config.json change. Restarting player to apply.")
                if not _blackout_proc or _blackout_proc.poll() is not None:
                    _blackout_proc = start_blackout(fullscreen)
                stop_player(proc)
                return 0

            # チャンネルフォルダ内が変わったらプレイリストを作り直して再起動
            if not channel_folder.exists():
                logger.info("Channel folder disappeared. Restarting.")
                if not _blackout_proc or _blackout_proc.poll() is not None:
                    _blackout_proc = start_blackout(fullscreen)
                stop_player(proc)
                return 0

            current_state = FolderState.from_folder(channel_folder)
            if current_state != last_folder_state:
                logger.info("Detected folder content change. Rebuilding playlist and restarting.")
                files_now = list_mp4_files(channel_folder)
                if not files_now:
                    logger.error("No mp4 files after change in %s", channel_folder)
                    if not _blackout_proc or _blackout_proc.poll() is not None:
                        _blackout_proc = start_blackout(fullscreen)
                    stop_player(proc)
                    return 0
                build_playlist(playlist_dir, active_channel, files_now)
                if not _blackout_proc or _blackout_proc.poll() is not None:
                    _blackout_proc = start_blackout(fullscreen)
                stop_player(proc)
                return 0
    finally:
        stop_player(proc)


def sleep_with_heartbeat(duration: float, heartbeat: HeartbeatState, *, error: str | None) -> None:
    end_at = time.monotonic() + duration
    while True:
        write_json_atomic(HEARTBEAT_PATH, heartbeat.to_payload(error=error, mpv_pid=None))
        remaining = end_at - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(HEARTBEAT_INTERVAL_SEC, max(0.1, remaining)))


def resolve_content_root(config: dict) -> Path:
    raw = config.get("content_root")
    if raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (ROOT / candidate).resolve()
        else:
            candidate = candidate.resolve()
        try:
            candidate.relative_to(CONTENT_DIR.resolve())
            return candidate
        except Exception:
            logger.warning("content_root outside content directory: %s", candidate)
    return CONTENT_DIR


def main() -> None:
    global _blackout_proc
    configure_logging(DEFAULT_LOG_DIR)
    logger.info("auto_play.py starting")
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    config_path = CONFIG_DIR / "config.json"
    active_path = CONFIG_DIR / "active.json"
    playlist_dir = CONFIG_DIR
    logger.info("Config path: %s", config_path)
    logger.info("Active path: %s", active_path)

    heartbeat = HeartbeatState()
    write_json_atomic(HEARTBEAT_PATH, heartbeat.to_payload(error="starting", mpv_pid=None))

    while True:
        try:
            config_mtime = safe_mtime(config_path)
            active_mtime = safe_mtime(active_path)

            config = load_json(config_path, {})
            log_dir = LOGS_DIR
            if log_dir != DEFAULT_LOG_DIR:
                configure_logging(log_dir)

            active = load_json(active_path, {})
            active_channel = active.get("active_channel")
            if not active_path.is_file() or not active_channel:
                raise ValueError(f"active.json not found or active_channel missing: {active_path}")

            content_root = resolve_content_root(config)
            fullscreen = bool(config.get("fullscreen", True))

            # 再生 + 監視（変更検知で戻ってくる）
            exit_code = run_player_with_watch(
                active_channel=active_channel,
                content_root=content_root,
                fullscreen=fullscreen,
                active_mtime_at_start=active_mtime,
                config_mtime_at_start=config_mtime,
                playlist_dir=playlist_dir,
                active_path=active_path,
                config_path=config_path,
                heartbeat=heartbeat,
            )

            # プレイヤーが自然終了した場合も再起動
            logger.error(
                "Player loop ended (code=%s). Restarting in %s seconds.",
                exit_code,
                RETRY_PLAYER_SECONDS,
            )
            write_json_atomic(
                HEARTBEAT_PATH,
                heartbeat.to_payload(error=f"player stopped ({exit_code})", mpv_pid=None),
            )
            sleep_with_heartbeat(RETRY_PLAYER_SECONDS, heartbeat, error=f"player stopped ({exit_code})")

        except Exception as exc:
            logger.exception("Unhandled error. Retrying in %s seconds.", RETRY_MISSING_SECONDS)
            if _blackout_proc and _blackout_proc.poll() is None:
                stop_player(_blackout_proc)
                _blackout_proc = None
            sleep_with_heartbeat(RETRY_MISSING_SECONDS, heartbeat, error=str(exc)[:200])


if __name__ == "__main__":
    main()
