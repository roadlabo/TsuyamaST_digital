import csv
import json
import os
import shutil
import time
from datetime import datetime
from typing import Callable, Optional

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
    if os.path.isfile(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(YEARLY_HEADER + "\n")


def sample_and_append(
    csv_path: str,
    yearly_path: str,
    state_path: str,
    ts_slot: str,
    logger: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    state = load_state(state_path)
    if state.get("last_written_ts") == ts_slot:
        return False, "same_ts"

    row = read_latest_row(csv_path)
    if not row:
        return False, "input_missing"

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
        return False, "row_short"

    cpu_usage = parse_float(row[INPUT_COLUMNS["cpu_usage"]])
    cpu_temp = parse_float(row[INPUT_COLUMNS["cpu_temp"]])
    pch_temp = parse_float(row[INPUT_COLUMNS["pch_temp"]])
    gpu_temp = parse_float(row[INPUT_COLUMNS["gpu_temp"]])
    ssd_temp = parse_float(row[INPUT_COLUMNS["ssd_temp"]])
    memory_temp = parse_float(row[INPUT_COLUMNS["memory_temp"]])

    if None in (cpu_usage, cpu_temp, pch_temp, gpu_temp, ssd_temp, memory_temp):
        return False, "missing_values"

    try:
        usage = shutil.disk_usage(r"C:\\")
        total_gb = round(usage.total / (1024**3), 1)
        free_gb = round(usage.free / (1024**3), 1)
    except Exception:
        total_gb = None
        free_gb = None

    if total_gb is None or free_gb is None:
        return False, "disk_unavailable"

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
    return True, "appended"


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
    base_dir: str,
    logger: Optional[Callable[[str], None]] = None,
    poll_interval_sec: float = 2.0,
) -> None:
    try:
        sample_minutes = int(os.getenv("HWINFO_SAMPLE_MINUTES", "30"))
    except Exception:
        sample_minutes = 30
    if sample_minutes <= 0:
        sample_minutes = 30

    logs_dir = os.path.join(base_dir, "logs", "hwinfo")
    input_csv = os.path.join(logs_dir, "hwinfo_sensors.csv")
    yearly_dir = os.path.join(logs_dir, "yearly")
    state_path = os.path.join(yearly_dir, "state.json")

    if logger:
        logger(
            "HWINFO yearly: start "
            f"input={input_csv} yearly_dir={yearly_dir} "
            f"columns={INPUT_COLUMNS} truncate=1048576 sample_minutes={sample_minutes}"
        )

    last_log = {"ts_slot": None, "reason": None}

    while True:
        try:
            now_dt = datetime.now()
            ts_slot = get_ts_slot(now_dt, sample_minutes)
            year = now_dt.year
            yearly_path = os.path.join(yearly_dir, f"hwinfo_{year}.csv")
            wrote, reason = sample_and_append(
                input_csv,
                yearly_path,
                state_path,
                ts_slot,
                logger=logger,
            )
            if logger and reason != "appended":
                if last_log["ts_slot"] != ts_slot or last_log["reason"] != reason:
                    logger(f"HWINFO yearly: skip ({reason}) {ts_slot}")
                    last_log = {"ts_slot": ts_slot, "reason": reason}
            truncate_if_needed(input_csv, logger=logger)
        except Exception as exc:
            if logger:
                logger(f"HWINFO yearly: loop error ({exc})")
        time.sleep(poll_interval_sec)
