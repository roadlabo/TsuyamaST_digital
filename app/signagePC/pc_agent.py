import argparse
import csv
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

try:
    import psutil
except ImportError:
    psutil = None

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"
CONFIG_DIR = APP_DIR / "config"
CONTENT_DIR = ROOT / "content"
LOGS_DIR = ROOT / "logs"
RUNTIME_DIR = ROOT / "runtime"

PYTHON_EXE = RUNTIME_DIR / "python" / "python.exe"
MPV_EXE = RUNTIME_DIR / "mpv" / "mpv.exe"
HWINFO_EXE = RUNTIME_DIR / "hwinfo" / "HWiNFO64.exe"

HWINFO_LOG_DIR = LOGS_DIR / "hwinfo"
HWINFO_CSV = HWINFO_LOG_DIR / "hwinfo_sensors.csv"
HWINFO_YEARLY_DIR = HWINFO_LOG_DIR / "yearly"
HWINFO_STATE_JSON = HWINFO_YEARLY_DIR / "state.json"


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


INPUT_COLUMNS = {
    "date": 0,
    "time": 1,
    "cpu_usage": 118,
    "cpu_temp": 156,
    "pch_temp": 337,
    "memory_temp": 338,
    "ssd_temp": 358,
    "gpu_temp": 373,
}

YEARLY_HEADER = (
    "日時,CPU使用率[%],CPU温度[℃],チップセット温度[℃],CPU内GPU温度[℃],"
    "SSD温度[℃],メモリ温度[℃],Cドライブ総容量[GB],Cドライブ空き容量[GB]"
)

ENCODINGS = ("utf-8-sig", "cp932", "utf-8")


def load_state(path: str) -> dict:
    if not os.path.isfile(path):
        return {"last_written_ts": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"last_written_ts": None}
        if "last_written_ts" not in data:
            data["last_written_ts"] = None
        return data
    except Exception:
        return {"last_written_ts": None}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def parse_datetime(date_value: str, time_value: str) -> Optional[datetime]:
    date_text = str(date_value or "").strip()
    time_text = str(time_value or "").strip()
    if not date_text or not time_text:
        return None

    candidates = [f"{date_text} {time_text}"]
    if "." in time_text:
        candidates.append(f"{date_text} {time_text.split('.', 1)[0]}")

    formats = (
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M",
    )
    for value in candidates:
        for fmt in formats:
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                continue
    return None


def get_ts_slot(now_dt: datetime, minutes: int) -> str:
    minute_slot = (now_dt.minute // minutes) * minutes
    slot_dt = now_dt.replace(minute=minute_slot, second=0, microsecond=0)
    return slot_dt.strftime("%Y/%m/%d %H:%M")


def read_latest_row(csv_path: str) -> Optional[list[str]]:
    if not os.path.isfile(csv_path):
        return None

    for encoding in ENCODINGS:
        try:
            with open(csv_path, "r", encoding=encoding, errors="replace") as f:
                reader = csv.reader(f)
                last_row = None
                for row in reader:
                    if row and any(cell.strip() for cell in row):
                        last_row = row
                if last_row:
                    return last_row
        except Exception:
            continue
    return None


def parse_float(value: str) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def ensure_yearly_file(path: str) -> None:
    yearly_path = Path(path)
    if yearly_path.is_file():
        return
    yearly_path.parent.mkdir(parents=True, exist_ok=True)
    with yearly_path.open("w", encoding="utf-8", newline="") as f:
        f.write(YEARLY_HEADER + "\n")


def sample_and_append(
    csv_path: str,
    yearly_dir: str,
    state_path: str,
    sample_minutes: int,
    logger: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str, Optional[str]]:
    row = read_latest_row(csv_path)
    if not row:
        return False, "input_missing", None

    required_indexes = [
        INPUT_COLUMNS["date"],
        INPUT_COLUMNS["time"],
        INPUT_COLUMNS["cpu_usage"],
        INPUT_COLUMNS["cpu_temp"],
        INPUT_COLUMNS["pch_temp"],
        INPUT_COLUMNS["gpu_temp"],
        INPUT_COLUMNS["ssd_temp"],
        INPUT_COLUMNS["memory_temp"],
    ]
    if max(required_indexes) >= len(row):
        return False, "row_short", None

    row_dt = parse_datetime(
        row[INPUT_COLUMNS["date"]],
        row[INPUT_COLUMNS["time"]],
    )
    if row_dt is None:
        return False, "bad_datetime", None

    ts_slot = get_ts_slot(row_dt, sample_minutes)
    state = load_state(state_path)
    if state.get("last_written_ts") == ts_slot:
        return False, "same_ts", ts_slot

    cpu_usage = parse_float(row[INPUT_COLUMNS["cpu_usage"]])
    cpu_temp = parse_float(row[INPUT_COLUMNS["cpu_temp"]])
    pch_temp = parse_float(row[INPUT_COLUMNS["pch_temp"]])
    gpu_temp = parse_float(row[INPUT_COLUMNS["gpu_temp"]])
    ssd_temp = parse_float(row[INPUT_COLUMNS["ssd_temp"]])
    memory_temp = parse_float(row[INPUT_COLUMNS["memory_temp"]])

    if None in (cpu_usage, cpu_temp, pch_temp, gpu_temp, ssd_temp, memory_temp):
        return False, "missing_values", ts_slot

    try:
        usage = shutil.disk_usage(r"C:\\")
        total_gb = round(usage.total / (1024**3), 1)
        free_gb = round(usage.free / (1024**3), 1)
    except Exception:
        total_gb = None
        free_gb = None

    if total_gb is None or free_gb is None:
        return False, "disk_unavailable", ts_slot

    yearly_path = os.path.join(yearly_dir, f"hwinfo_{row_dt.year}.csv")
    ensure_yearly_file(yearly_path)
    with open(yearly_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                ts_slot,
                cpu_usage,
                cpu_temp,
                pch_temp,
                gpu_temp,
                ssd_temp,
                memory_temp,
                total_gb,
                free_gb,
            ]
        )

    state["last_written_ts"] = ts_slot
    save_state(state_path, state)
    if logger:
        logger(f"HWINFO yearly: appended {ts_slot}")
    return True, "appended", ts_slot


def truncate_if_needed(
    csv_path: str,
    max_bytes: int = 1_048_576,
    logger: Optional[Callable[[str], None]] = None,
) -> bool:
    if not os.path.isfile(csv_path):
        return False
    try:
        size = os.path.getsize(csv_path)
    except Exception:
        return False
    if size <= max_bytes:
        return False

    try:
        with open(csv_path, "r+", encoding="utf-8", errors="ignore") as f:
            f.truncate(0)
        if logger:
            logger(f"HWINFO yearly: truncated input CSV (size={size})")
        return True
    except Exception as exc:
        if logger:
            logger(f"HWINFO yearly: truncate failed ({exc})")
        return False


def run_hwinfo_yearly_logger(
    logger: Optional[Callable[[str], None]] = None,
    poll_interval_sec: float = 2.0,
) -> None:
    try:
        sample_minutes = int(os.getenv("HWINFO_SAMPLE_MINUTES", "30"))
    except Exception:
        sample_minutes = 30
    if sample_minutes <= 0:
        sample_minutes = 30

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    HWINFO_LOG_DIR.mkdir(parents=True, exist_ok=True)
    HWINFO_YEARLY_DIR.mkdir(parents=True, exist_ok=True)

    input_csv = str(HWINFO_CSV)
    yearly_dir = str(HWINFO_YEARLY_DIR)
    state_path = str(HWINFO_STATE_JSON)

    if logger:
        logger(
            "HWINFO yearly: start "
            f"input={input_csv} yearly_dir={yearly_dir} "
            f"columns={INPUT_COLUMNS} truncate=1048576 sample_minutes={sample_minutes}"
        )

    last_log = {"ts_slot": None, "reason": None}

    while True:
        try:
            wrote, reason, ts_slot = sample_and_append(
                input_csv,
                yearly_dir,
                state_path,
                sample_minutes,
                logger=logger,
            )
            if logger and reason != "appended":
                if last_log["ts_slot"] != ts_slot or last_log["reason"] != reason:
                    log_ts = ts_slot or "-"
                    logger(f"HWINFO yearly: skip ({reason}) {log_ts}")
                    last_log = {"ts_slot": ts_slot, "reason": reason}
            truncate_if_needed(input_csv, logger=logger)
        except Exception as exc:
            if logger:
                logger(f"HWINFO yearly: loop error ({exc})")
        time.sleep(poll_interval_sec)


def run_powershell_json(ps_script: str, timeout: int = 5):
    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ps_script,
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout).strip())
    out = cp.stdout.strip()
    if not out:
        return None
    return json.loads(out)


def get_ssd_temp_c_via_lhm(hardware_hint: str = ""):
    """
    LibreHardwareMonitor の WMI から SSD温度を取得する。
    - hardware_hint があれば、その文字列を含む Hardware/Name を優先
    - 見つからなければ (None, None, None) を返す
    """
    # PowerShell側で「SSDっぽい温度」を優先して1件返す
    ps = r"""
    $ErrorActionPreference = "SilentlyContinue"
    $ns = "root\LibreHardwareMonitor"
    $hint = "%HINT%"
    $sensors = Get-CimInstance -Namespace $ns -ClassName Sensor
    if (-not $sensors) { "" | Out-String; exit 0 }

    $temps = $sensors | Where-Object { $_.SensorType -eq "Temperature" }

    # まず hint（例: SSSTC）があれば最優先
    if ($hint -and $hint.Trim().Length -gt 0) {
      $cand = $temps | Where-Object { ($_.Name -match $hint) -or ($_.Hardware -match $hint) } | Select-Object -First 1
      if ($cand) {
        @{ ok=$true; source="lhm"; name=$cand.Name; temp_c=[double]$cand.Value } | ConvertTo-Json -Compress
        exit 0
      }
    }

    # 次に SSD/NVMe/Drive っぽい名前を優先
    $cand2 = $temps | Where-Object { $_.Name -match "SSD|NVMe|Drive" -or $_.Hardware -match "SSD|NVMe|Drive" } | Select-Object -First 1
    if ($cand2) {
      @{ ok=$true; source="lhm"; name=$cand2.Name; temp_c=[double]$cand2.Value } | ConvertTo-Json -Compress
      exit 0
    }

    "" | Out-String
    """
    ps = ps.replace("%HINT%", (hardware_hint or "").replace('"', ""))
    data = run_powershell_json(ps, timeout=5)
    if not data or not data.get("ok"):
        return None, None, None

    try:
        temp = float(data["temp_c"])
    except Exception:
        return None, None, None

    # 現実的でない温度は無効扱い
    if temp < -20.0 or temp > 120.0:
        return None, None, None

    return temp, str(data.get("name") or "SSD"), "lhm"


def get_cpu_temp_c_via_lhm():
    ps = r"""
    $ErrorActionPreference = "SilentlyContinue"
    $ns = "root\LibreHardwareMonitor"
    $sensors = Get-CimInstance -Namespace $ns -ClassName Sensor
    if (-not $sensors) { "" | Out-String; exit 0 }

    $temps = $sensors | Where-Object { $_.SensorType -eq "Temperature" }

    $pkg = $temps | Where-Object { $_.Name -match "CPU Package" } | Select-Object -First 1
    if ($pkg) {
      @{ ok=$true; source="lhm"; name=$pkg.Name; temp_c=[double]$pkg.Value } | ConvertTo-Json -Compress
      exit 0
    }

    $cpuTemps = $temps | Where-Object { $_.Name -match "CPU" }
    if ($cpuTemps) {
      $max = ($cpuTemps | Measure-Object -Property Value -Maximum).Maximum
      $one = $cpuTemps | Sort-Object Value -Descending | Select-Object -First 1
      @{ ok=$true; source="lhm"; name=$one.Name; temp_c=[double]$max } | ConvertTo-Json -Compress
      exit 0
    }

    "" | Out-String
    """
    data = run_powershell_json(ps, timeout=5)
    if not data or not data.get("ok"):
        return None, None
    temp = float(data["temp_c"])
    if temp < -20.0 or temp > 120.0:
        return None, None
    return temp, str(data.get("name") or "CPU")


def get_cpu_temp_c_via_acpi():
    ps = r"""
    $ErrorActionPreference = "SilentlyContinue"
    $t = Get-CimInstance -Namespace root\wmi -ClassName MSAcpi_ThermalZoneTemperature | Select-Object -First 1
    if (-not $t) { "" | Out-String; exit 0 }
    $c = ($t.CurrentTemperature / 10) - 273.15
    @{ ok=$true; source="acpi"; name="ThermalZone"; temp_c=[double]$c } | ConvertTo-Json -Compress
    """
    data = run_powershell_json(ps, timeout=5)
    if not data or not data.get("ok"):
        return None, None
    temp = float(data["temp_c"])
    # 現実的なCPU/筐体温度範囲外は無効値として捨てる
    if temp < -20.0 or temp > 120.0:
        return None, None
    return temp, str(data.get("name") or "ThermalZone")


def get_cpu_temp_c():
    try:
        t, name = get_cpu_temp_c_via_lhm()
        if t is not None:
            return t, name, "lhm"
    except Exception:
        pass
    try:
        t, name = get_cpu_temp_c_via_acpi()
        if t is not None:
            return t, name, "acpi"
    except Exception:
        pass
    return None, None, None


def write_status(status_path: str, payload: dict) -> None:
    tmp = status_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, status_path)


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_move(src: str, dst: str) -> None:
    try:
        os.replace(src, dst)
    except Exception:
        pass


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

    status_dir = LOGS_DIR / "status"
    config_dir = CONFIG_DIR
    logs_dir = LOGS_DIR

    ensure_dir(status_dir)
    ensure_dir(config_dir)
    ensure_dir(logs_dir)
    ensure_dir(HWINFO_LOG_DIR)
    ensure_dir(HWINFO_YEARLY_DIR)

    status_path = str(status_dir / "pc_status.json")
    cmd_path = str(config_dir / "command.json")
    cmd_result_path = str(status_dir / "command_result.json")
    log_path = str(logs_dir / "pc_agent.log")

    if psutil is None:
        log_line(log_path, "WARN: psutil is not installed. CPU/mem/disk metrics may be missing.")

    threading.Thread(
        target=run_hwinfo_yearly_logger,
        kwargs={"logger": lambda msg: log_line(log_path, msg)},
        daemon=True,
    ).start()

    hostname = socket.gethostname()

    if psutil:
        try:
            psutil.cpu_percent(interval=None)  # warmup
        except Exception:
            pass

    interval = max(1, int(args.interval))

    while True:
        try:
            cpu_percent = None
            ssd_usage_percent = None

            if psutil:
                # CPU TOTAL (%)
                cpu_percent = psutil.cpu_percent(interval=0.2)

                # SSD使用率は C:\ 固定（曖昧さ排除）
                try:
                    du = psutil.disk_usage(r"C:\\")
                    ssd_usage_percent = float(du.percent)
                except Exception:
                    ssd_usage_percent = None

            # SSD温度（取れなければNoneで継続）
            ssd_temp_c, ssd_temp_sensor, ssd_temp_source = get_ssd_temp_c_via_lhm(
                hardware_hint="SSSTC"
            )

            payload = {
                "timestamp": now_iso(),
                "host": hostname,
                "cpu_total_percent": cpu_percent,
                "ssd": {
                    "drive": r"C:\\",
                    "usage_percent": ssd_usage_percent,
                    "temp_c": ssd_temp_c,
                    "temp_sensor": ssd_temp_sensor,
                    "temp_source": ssd_temp_source,
                },
                "source": {
                    "cpu_total_percent": "psutil" if psutil else None,
                    "ssd_usage_percent": "psutil" if psutil else None,
                    "ssd_temp_c": "lhm" if ssd_temp_source == "lhm" else None,
                },
            }
            write_status(status_path, payload)

            if os.path.isfile(cmd_path):
                try:
                    cmd = read_json(cmd_path)
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

                    write_status(cmd_result_path, result)
                    done_path = os.path.join(config_dir, f"command.done.{int(time.time())}.json")
                    safe_move(cmd_path, done_path)

                except Exception as e:
                    log_line(log_path, f"Command handling error: {e}")

        except Exception as e:
            log_line(log_path, f"Loop error: {e}")

        time.sleep(interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
