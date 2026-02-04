import argparse
import json
import os
import shutil
import socket
import subprocess
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    import psutil
except ImportError:
    psutil = None

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from common.json_io import write_json_safe
CONFIG_DIR = APP_DIR / "config"
CONTENT_DIR = ROOT / "content"
LOGS_DIR = ROOT / "logs"
RUNTIME_DIR = ROOT / "runtime"
STATUS_DIR = LOGS_DIR / "status"

PYTHON_EXE = RUNTIME_DIR / "python" / "python.exe"


def now_iso() -> str:
    return datetime.now(JST).isoformat()


def ensure_dir(path: str | Path) -> None:
    os.makedirs(path, exist_ok=True)


def log_line(log_path: str, msg: str) -> None:
    line = f"{now_iso()} {msg}\n"

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def write_json_status(path: str | Path, payload: dict, log_path: Path) -> None:
    ok, retry_count, err = write_json_safe(path, payload, indent=2, ensure_ascii=False)
    if ok and retry_count > 0:
        log_line(str(log_path), f"WARN: JSON write retry succeeded ({retry_count} retries): {path}")
    if not ok:
        log_line(str(log_path), f"ERROR: JSON write failed after retries: {path} ({err})")


def read_json_safe(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def read_json_with_error(path: Path, *, retries: int = 3) -> tuple[Optional[dict], Optional[str]]:
    if not path.is_file():
        return None, "missing"

    last_err: Exception | None = None
    for i in range(max(1, int(retries))):
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f), None
        except (PermissionError, OSError) as e:
            last_err = e
            time.sleep(min(0.2, 0.05 * (2 ** i)))
            continue
        except json.JSONDecodeError as e:
            last_err = e
            time.sleep(min(0.2, 0.05 * (2 ** i)))
            continue
        except Exception as e:
            last_err = e
            break

    # ここに来たら読めなかった
    return None, "parse_error"


def parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=JST)
    return parsed


def get_mem_used_percent() -> Optional[float]:
    if psutil is None:
        return None
    try:
        return float(psutil.virtual_memory().percent)
    except Exception:
        return None


def get_os_uptime_sec() -> Optional[int]:
    if psutil is None:
        return None
    try:
        return int(time.time() - float(psutil.boot_time()))
    except Exception:
        return None


def find_auto_play_process() -> dict:
    if psutil is None:
        return {"running": None, "pid": None, "started_at": None}
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if any("auto_play.py" in str(part) for part in cmdline):
                started_at = None
                created = proc.info.get("create_time")
                if created:
                    started_at = datetime.fromtimestamp(float(created), JST).isoformat()
                return {"running": True, "pid": proc.info.get("pid"), "started_at": started_at}
        except Exception:
            continue
    return {"running": False, "pid": None, "started_at": None}


def read_player_heartbeat(path: Path, now_dt: datetime, stale_sec: int = 60) -> dict:
    heartbeat, error = read_json_with_error(path)
    base = {
        "alive": False,
        "reason": error or "missing",
        "timestamp": None,
        "current_file": None,
        "loop_count": None,
        "last_change_at": None,
        "mpv_pid": None,
        "error": None,
    }
    if not heartbeat:
        return base

    ts_raw = heartbeat.get("timestamp")
    ts = parse_iso_timestamp(ts_raw)
    if ts is None:
        base["reason"] = "parse_error"
        base["timestamp"] = ts_raw
        return base

    age_sec = (now_dt - ts).total_seconds()
    alive = age_sec <= stale_sec
    result = {
        "alive": alive,
        "timestamp": ts_raw,
        "current_file": heartbeat.get("current_file"),
        "loop_count": heartbeat.get("loop_count"),
        "last_change_at": heartbeat.get("last_change_at"),
        "mpv_pid": heartbeat.get("mpv_pid"),
        "error": heartbeat.get("error"),
    }
    if not alive:
        result["reason"] = "stale"
    return result


def exec_shutdown(action: str) -> int:
    if action == "shutdown":
        cp = subprocess.run(["shutdown", "/s", "/t", "0"], capture_output=True, text=True)
        return cp.returncode
    if action == "reboot":
        cp = subprocess.run(["shutdown", "/r", "/t", "0"], capture_output=True, text=True)
        return cp.returncode
    raise ValueError("unknown action")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=5)
    args = ap.parse_args()

    status_dir = STATUS_DIR
    config_dir = CONFIG_DIR
    logs_dir = LOGS_DIR

    ensure_dir(status_dir)
    ensure_dir(config_dir)
    ensure_dir(logs_dir)

    status_path = status_dir / "pc_status.json"
    cmd_path = config_dir / "command.json"
    cmd_result_path = status_dir / "command_result.json"
    log_path = logs_dir / "pc_agent.log"
    heartbeat_path = status_dir / "pc_agent_heartbeat.json"
    player_heartbeat_path = status_dir / "auto_play_heartbeat.json"

    if psutil is None:
        log_line(log_path, "WARN: psutil is not installed. CPU/mem/disk metrics may be missing.")

    hostname = socket.gethostname()
    agent_started_at = time.time()

    if psutil:
        try:
            psutil.cpu_percent(interval=None)  # warmup
        except Exception:
            pass

    interval = max(1, int(args.interval))

    while True:
        try:
            now_dt = datetime.now(JST)
            agent_uptime_sec = int(time.time() - agent_started_at)
            os_uptime_sec = get_os_uptime_sec()

            cpu_percent = None
            cpu_percent_source = "none"
            mem_used_percent = None
            mem_used_source = "none"
            ssd_usage_percent = None
            ssd_used_gb = None
            ssd_total_gb = None
            ssd_usage_source = "none"

            if psutil:
                # CPU TOTAL (%)
                cpu_percent = psutil.cpu_percent(interval=0.2)
                cpu_percent_source = "psutil"

                # MEMORY (% used)
                mem_used_percent = get_mem_used_percent()
                mem_used_source = "psutil" if mem_used_percent is not None else "none"

                # SSD usage C:\
                try:
                    du = psutil.disk_usage(r"C:\\")
                    ssd_usage_percent = float(du.percent)
                    ssd_usage_source = "psutil"
                except Exception:
                    ssd_usage_percent = None

            try:
                du = shutil.disk_usage(r"C:\\")
                ssd_used_gb = round(du.used / (1024**3), 1)
                ssd_total_gb = round(du.total / (1024**3), 1)
            except Exception:
                ssd_used_gb = None
                ssd_total_gb = None

            if cpu_percent is None:
                cpu_percent_source = "none"

            auto_play_status = find_auto_play_process()
            player_status = read_player_heartbeat(player_heartbeat_path, now_dt)

            payload = {
                "timestamp": now_iso(),
                "host": hostname,
                "cpu_total_percent": cpu_percent,
                "mem_used_percent": mem_used_percent,
                "agent_uptime_sec": agent_uptime_sec,
                "os_uptime_sec": os_uptime_sec,
                "ssd": {
                    "drive": r"C:\\",
                    "usage_percent": ssd_usage_percent,
                    "used_gb": ssd_used_gb,
                    "total_gb": ssd_total_gb,
                },
                "auto_play": auto_play_status,
                "player": player_status,
                "source": {
                    "cpu_total_percent": cpu_percent_source,
                    "mem_used_percent": mem_used_source,
                    "ssd_usage_percent": ssd_usage_source,
                    "ssd_used_gb": "shutil" if ssd_used_gb is not None else "none",
                    "ssd_total_gb": "shutil" if ssd_total_gb is not None else "none",
                },
            }
            write_json_status(status_path, payload, log_path)

            heartbeat_payload = {
                "timestamp": now_iso(),
                "host": hostname,
                "pid": os.getpid(),
                "agent_uptime_sec": agent_uptime_sec,
                "os_uptime_sec": os_uptime_sec,
            }
            write_json_status(heartbeat_path, heartbeat_payload, log_path)

            if os.path.isfile(cmd_path):
                try:
                    cmd = read_json_safe(Path(cmd_path)) or {}
                    action = (cmd.get("action") or cmd.get("command") or "").lower().strip()
                    force = bool(cmd.get("force", False))

                    if action in ("shutdown", "reboot") and force:
                        rc = exec_shutdown(action)
                        result = {
                            "timestamp": now_iso(),
                            "action": action,
                            "executed": True,
                            "returncode": rc,
                            "note": "command executed",
                        }
                    else:
                        result = {
                            "timestamp": now_iso(),
                            "action": action,
                            "executed": False,
                            "returncode": None,
                            "note": "ignored (action invalid or force=false)",
                        }

                    write_json_status(cmd_result_path, result, log_path)
                    done_path = os.path.join(config_dir, f"command.done.{int(time.time())}.json")
                    try:
                        os.replace(cmd_path, done_path)
                    except Exception:
                        pass

                except Exception as e:
                    log_line(log_path, f"Command handling error: {e}")

        except Exception as e:
            log_line(log_path, f"Loop error: {e}")

        time.sleep(interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
