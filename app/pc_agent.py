import argparse
import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta

try:
    import psutil
except ImportError:
    psutil = None

from hwinfo.hwinfo_yearly_logger import run_hwinfo_yearly_logger


JST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(JST).isoformat()


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def log_line(log_path: str, msg: str) -> None:
    line = f"{now_iso()} {msg}\n"

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


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


def start_lhm_if_exists(lhm_exe_path: str, log_path: str) -> None:
    if not lhm_exe_path:
        return
    if not os.path.isfile(lhm_exe_path):
        return

    try:
        task = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq LibreHardwareMonitor.exe"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if "LibreHardwareMonitor.exe" in task.stdout:
            return
    except Exception:
        pass

    try:
        subprocess.Popen(
            [lhm_exe_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log_line(log_path, f"Started LibreHardwareMonitor: {lhm_exe_path}")
    except Exception as e:
        log_line(log_path, f"Failed to start LibreHardwareMonitor: {e}")


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
    ap.add_argument("--base", default=r"C:\_TsuyamaSignage")
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument(
        "--lhm",
        default=r"C:\_TsuyamaSignage\bin\LibreHardwareMonitor\LibreHardwareMonitor.exe",
    )
    args = ap.parse_args()

    base = args.base
    status_dir = os.path.join(base, "status")
    config_dir = os.path.join(base, "config")
    logs_dir = os.path.join(base, "logs")

    ensure_dir(status_dir)
    ensure_dir(config_dir)
    ensure_dir(logs_dir)

    status_path = os.path.join(status_dir, "pc_status.json")
    cmd_path = os.path.join(config_dir, "command.json")
    cmd_result_path = os.path.join(status_dir, "command_result.json")
    log_path = os.path.join(logs_dir, "pc_agent.log")

    if psutil is None:
        log_line(log_path, "WARN: psutil is not installed. CPU/mem/disk metrics may be missing.")

    start_lhm_if_exists(args.lhm, log_path)
    threading.Thread(
        target=run_hwinfo_yearly_logger,
        args=(base,),
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
                    action = (cmd.get("action") or "").lower().strip()
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
