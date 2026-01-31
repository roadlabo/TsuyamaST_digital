import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT_DIR / "config"
LOG_DIR = ROOT_DIR / "logs"
STATE_PATH = CONFIG_DIR / "power_agent_state.json"
COMMAND_PATH = CONFIG_DIR / "command.json"
RESULT_PATH = CONFIG_DIR / "command_result.json"

LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        logging.exception("Failed to parse %s", path)
        return default


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    bak_path = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        try:
            shutil.copy2(path, bak_path)
        except OSError:
            pass
    with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)


def setup_logging():
    log_path = LOG_DIR / f"power_agent_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def run_command(command: str) -> Tuple[bool, str]:
    try:
        if command == "reboot":
            subprocess.Popen(["shutdown", "/r", "/t", "0"], shell=False)
        elif command == "shutdown":
            subprocess.Popen(["shutdown", "/s", "/t", "0"], shell=False)
        else:
            return False, f"unknown command: {command}"
        return True, ""
    except Exception as exc:
        logging.exception("Command failed")
        return False, str(exc)


def write_result(command_id: str, status: str, message: str = "") -> None:
    payload = {
        "command_id": command_id,
        "status": status,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": message,
    }
    write_json_atomic(RESULT_PATH, payload)


def main():
    setup_logging()
    logging.info("PowerAgent started")
    state = load_json(STATE_PATH, {"last_command_id": ""})

    while True:
        command_data = load_json(COMMAND_PATH, None)
        if command_data and command_data.get("command_id"):
            command_id = command_data.get("command_id")
            if command_id != state.get("last_command_id"):
                logging.info("New command received: %s", command_data)
                write_result(command_id, "accepted", "")
                ok, message = run_command(command_data.get("command"))
                status = "ok" if ok else "ng"
                write_result(command_id, status, message)
                state["last_command_id"] = command_id
                write_json_atomic(STATE_PATH, state)
        time.sleep(2)


if __name__ == "__main__":
    main()
