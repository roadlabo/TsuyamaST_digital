import faulthandler
import io
import json
import importlib.util
import inspect
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time as time_module
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

try:
    from PySide6 import QtMultimedia, QtMultimediaWidgets
    HAS_QTMULTIMEDIA = True
except Exception:
    QtMultimedia = None
    QtMultimediaWidgets = None
    HAS_QTMULTIMEDIA = False

if importlib.util.find_spec("cv2"):
    import cv2
else:
    cv2 = None

if importlib.util.find_spec("watchdog"):
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    WATCHDOG_AVAILABLE = True
else:
    FileSystemEventHandler = None
    Observer = None
    WATCHDOG_AVAILABLE = False

APP_NAME = "TsuyamaST SuperAI Signage Controller"

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT_DIR / "config"
CONTENT_DIR = ROOT_DIR.parent / "content"
LOG_DIR = ROOT_DIR.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TELEMETRY_LOCAL_PATH = Path(r"C:\_TsuyamaSignage\app\logs\telemetry_local.json")

REMOTE_APP_DIR = "app"
REMOTE_CONFIG_DIR = f"{REMOTE_APP_DIR}\\config"
REMOTE_LOGS_DIR = "logs"
REMOTE_CONTENT_DIR = "content"

INVENTORY_PATH = CONFIG_DIR / "inventory.json"
AI_STATUS_PATH = CONFIG_DIR / "ai_status.json"
SETTINGS_PATH = CONFIG_DIR / "controller_settings.json"

BASE_COL = 1
N_SIGNAGE = 20
CHANNELS = [f"ch{idx:02d}" for idx in range(1, N_SIGNAGE + 1)]
NORMAL_CHOICES = [f"ch{n:02d}" for n in range(5, 11)]
EMERGENCY_CHANNEL = "ch20"
TIMER_CHOICES = [f"ch{n:02d}" for n in range(11, 20)]
AI_CHOICES = ["通常時と同じ", "ch02", "ch03", "ch04"]
SLEEP_FIXED = "ch01"
TIMER_CHANNEL_COLORS = {
    "ch11": QtGui.QColor(220, 50, 32),
    "ch12": QtGui.QColor(255, 140, 0),
    "ch13": QtGui.QColor(255, 215, 0),
    "ch14": QtGui.QColor(154, 205, 50),
    "ch15": QtGui.QColor(34, 139, 34),
    "ch16": QtGui.QColor(0, 191, 255),
    "ch17": QtGui.QColor(30, 144, 255),
    "ch18": QtGui.QColor(138, 43, 226),
    "ch19": QtGui.QColor(75, 0, 130),
    "ch20": QtGui.QColor(255, 105, 180),
}
LEFT_COL_WIDTH = 170
GAP_PX = 3
OUTER_MARGIN = 6
PC_STATUS_ROW_HEIGHT = 18
PC_STATUS_FONT_SIZE = 8
PC_STATUS_ITEMS = [
    ("last_update", "最終更新"),
    ("playback_state", "再生状態"),
    ("cpu_usage", "CPU使用率[%]"),
    ("mem_usage", "メモリ使用率[%]"),
    ("c_drive", "Cドライブ（使用/全体）"),
]


@dataclass
class SignState:
    name: str
    ip: str
    exists: bool
    share_name: str
    enabled: bool = True
    online: bool = False
    last_error: str = ""
    last_update: Optional[str] = None
    active_channel: Optional[str] = None


def load_json(path: Path, default):
    return safe_read_json(path, default, retries=3)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


# --- robust IO helpers (for SMB/AV/WinError5) -------------------------------

def _sleep_backoff(i: int, cap: float = 0.5) -> None:
    time_module.sleep(min(cap, 0.05 * (2 ** i)))


def run_hidden(*args, **kwargs) -> subprocess.CompletedProcess:
    if "stdout" not in kwargs:
        kwargs["stdout"] = subprocess.PIPE
    if "stderr" not in kwargs:
        kwargs["stderr"] = subprocess.PIPE
    if os.name == "nt":
        creationflags = kwargs.pop("creationflags", 0)
        kwargs["creationflags"] = creationflags | subprocess.CREATE_NO_WINDOW
        startupinfo = kwargs.get("startupinfo")
        if startupinfo is None:
            startupinfo = subprocess.STARTUPINFO()
            kwargs["startupinfo"] = startupinfo
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    return subprocess.run(*args, **kwargs)


def is_reachable(ip: str) -> bool:
    if not ip:
        return False
    try:
        result = run_hidden(
            ["ping", "-n", "1", "-w", "300", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def safe_replace(tmp_path: Path, dst_path: Path, *, retries: int = 10) -> None:
    """
    Windows/SMB/AV環境では、読み取り側が一瞬掴むだけで os.replace / Path.replace が WinError 5 で失敗することがある。
    短いリトライで吸収する。
    """
    last_exc = None
    for i in range(max(1, int(retries))):
        try:
            os.replace(tmp_path, dst_path)
            return
        except (PermissionError, OSError) as exc:
            last_exc = exc
            _sleep_backoff(i)
    # 最後に Path.replace も試す（環境差対策）
    try:
        tmp_path.replace(dst_path)
        return
    except Exception as exc:
        raise RuntimeError(
            f"safe_replace failed: {tmp_path} -> {dst_path} ({last_exc or exc})"
        ) from exc


def safe_read_json(path: Path, default, *, retries: int = 3):
    """
    書き換え中の読み取りで JSONDecodeError / PermissionError が起こり得るので、短いリトライで吸収する。
    """
    if not path.exists():
        return default
    last_exc = None
    for i in range(max(1, int(retries))):
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError,):
            return default
        except (json.JSONDecodeError, PermissionError, OSError) as exc:
            last_exc = exc
            _sleep_backoff(i, cap=0.2)
            continue
        except Exception as exc:
            last_exc = exc
            break
    logging.warning("safe_read_json failed: %s (%s)", path, last_exc)
    return default


def stat_fingerprint(path: Path) -> Tuple[int, int, int]:
    st = path.stat()
    mtime = int(st.st_mtime * 1000)
    ctime = int(getattr(st, "st_ctime", st.st_mtime) * 1000)
    size = int(st.st_size)
    return mtime, ctime, size


def is_same_file(master: Path, remote: Path, compare_ctime: bool = True) -> bool:
    try:
        m_mtime, m_ctime, m_size = stat_fingerprint(master)
        r_mtime, r_ctime, r_size = stat_fingerprint(remote)
    except FileNotFoundError:
        return False
    if compare_ctime:
        return (m_mtime == r_mtime) and (m_ctime == r_ctime) and (m_size == r_size)
    return (m_mtime == r_mtime) and (m_size == r_size)


def copy_file_atomic(src: Path, dst: Path) -> None:
    ensure_dir(dst.parent)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.exists():
        try:
            tmp.unlink()
        except Exception:
            pass
    shutil.copy2(src, tmp)
    bak = dst.with_suffix(dst.suffix + ".bak")
    try:
        if dst.exists():
            try:
                if bak.exists():
                    bak.unlink()
            except Exception:
                pass
            try:
                dst.replace(bak)
            except Exception:
                pass
    except Exception:
        pass
    safe_replace(tmp, dst, retries=12)


SYNC_EXTS = {".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp"}
SYNC_SAMPLE_SUFFIX = "_sample.mp4"


def _is_sample_video(name: str) -> bool:
    return name.lower().endswith(SYNC_SAMPLE_SUFFIX)


def sync_mirror_dir(
    master_dir: Path,
    remote_dir: Path,
    logger=None,
    dry_run: bool = False,
    compare_ctime: bool = True,
) -> Dict[str, int]:
    result = {"copied": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}
    ensure_dir(remote_dir)

    master_files: Dict[str, int] = {}
    for entry in master_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in SYNC_EXTS:
            continue
        if _is_sample_video(entry.name):
            continue
        master_files[entry.name] = entry.stat().st_size

    remote_files: Dict[str, int] = {}
    for entry in remote_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in SYNC_EXTS:
            continue
        if _is_sample_video(entry.name):
            continue
        remote_files[entry.name] = entry.stat().st_size

    to_copy: List[str] = []
    for name, msize in master_files.items():
        tsize = remote_files.get(name)
        if tsize is None or tsize != msize:
            to_copy.append(name)
        else:
            if logger:
                logger(f"[SKIP] {name}")
            result["skipped"] += 1

    to_delete = [name for name in remote_files.keys() if name not in master_files]

    for name in sorted(to_copy):
        src = master_dir / name
        dst = remote_dir / name
        try:
            if logger:
                logger(f"[COPY] {name}")
            if not dry_run:
                copy_file_atomic(src, dst)
            if name in remote_files:
                result["updated"] += 1
            else:
                result["copied"] += 1
        except Exception as exc:
            if logger:
                logger(f"[ERR] copy {name}: {repr(exc)}")
            result["errors"] += 1

    for name in sorted(to_delete):
        try:
            if logger:
                logger(f"[DEL] {name}")
            if not dry_run:
                (remote_dir / name).unlink()
            result["deleted"] += 1
        except Exception as exc:
            if logger:
                logger(f"[ERR] delete {name}: {exc}")
            result["errors"] += 1

    return result

def write_json_atomic(path: Path, payload: dict) -> None:
    ensure_dir(path.parent)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    bak_path = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        shutil.copy2(path, bak_path)
    with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    safe_replace(tmp_path, path, retries=10)


def write_json_atomic_remote(path: Path, payload: dict) -> None:
    """
    UNC(ネットワーク共有)向け: 親ディレクトリ作成はしない。
    （リモート側のフォルダ構成は前提として存在する）
    """
    last_exc = None
    for i in range(10):
        try:
            bak_path = path.with_suffix(path.suffix + ".bak")
            try:
                if path.exists():
                    shutil.copy2(path, bak_path)
            except Exception:
                pass
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            return
        except (PermissionError, OSError) as exc:
            last_exc = exc
            _sleep_backoff(i)
    logging.error("write_json_remote_overwrite failed: %s (%s)", path, last_exc)


def parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def normalize_hhmm(text: str) -> str:
    s = (text or "").strip()
    s = s.replace("：", ":")
    if ":" in s:
        parts = s.split(":")
        if len(parts) != 2:
            raise ValueError("時刻形式が不正です")
        hh = parts[0].zfill(2)
        mm = parts[1].zfill(2)
    else:
        if len(s) != 4 or not s.isdigit():
            raise ValueError("時刻は 00:00 または 0000 形式で入力してください")
        hh, mm = s[:2], s[2:]
    h = int(hh)
    m = int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("時刻の範囲が不正です")
    return f"{h:02d}:{m:02d}"


def time_in_range(now: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def build_unc_path(ip: str, share: str, relative: str) -> str:
    rel = relative.replace("/", "\\")
    return rf"\\{ip}\{share}\{rel}"


def read_config(sign_dir: Path) -> dict:
    return load_json(sign_dir / "config.json", {})


def read_active(sign_dir: Path) -> dict:
    return load_json(sign_dir / "active.json", {"active_channel": None})


def compute_active_channel(
    sign_config: dict,
    ai_status: dict,
    now: datetime,
) -> str:
    current_time = now.time()
    for window in sign_config.get("sleep_rules", []):
        try:
            start = parse_time(window["start"])
            end = parse_time(window["end"])
            if time_in_range(current_time, start, end):
                return sign_config.get("sleep_channel", "ch01")
        except Exception:
            continue

    level = int(ai_status.get("congestion_level", 1))
    if level >= 2:
        ai_channels = sign_config.get("ai_channels", {})
        key = f"level{level}"
        ai_choice = ai_channels.get(key)
        if ai_choice == "same_as_normal":
            return sign_config.get("normal_channel", "ch05")
        if ai_choice:
            return ai_choice

    matched_channel = None
    for rule in sign_config.get("timer_rules", []):
        try:
            start = parse_time(rule["start"])
            end = parse_time(rule["end"])
            if time_in_range(current_time, start, end):
                matched_channel = rule.get("channel")
        except Exception:
            continue

    if matched_channel:
        return matched_channel

    return sign_config.get("normal_channel", "ch05")


if WATCHDOG_AVAILABLE:
    class AiStatusHandler(FileSystemEventHandler):
        def __init__(self, callback):
            super().__init__()
            self._callback = callback

        def on_modified(self, event):
            if event.src_path.endswith("ai_status.json"):
                self._callback()


class TimeNormalizeDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, table: QtWidgets.QTableWidget, parent=None):
        super().__init__(parent)
        self._table = table

    def createEditor(self, parent, option, index):
        editor = QtWidgets.QLineEdit(parent)
        editor.setProperty("original", index.data() or "")
        return editor

    def setModelData(self, editor, model, index):
        text = editor.text()
        original = editor.property("original") or ""
        try:
            normalized = normalize_hhmm(text)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self._table, "入力エラー", str(exc))
            model.setData(index, original)
            QtCore.QTimer.singleShot(0, lambda: self._table.edit(index))
            return
        model.setData(index, normalized)


class ConfigDialog(QtWidgets.QDialog):
    def __init__(self, sign_name: str, config: dict, parent=None, controller_window=None):
        super().__init__(parent)
        self.setWindowTitle(f"{sign_name} 設定")
        self.config = config
        self.controller_window = controller_window
        self.sign_name = sign_name

        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()

        self.sleep_combo = QtWidgets.QComboBox()
        self.sleep_combo.addItems([SLEEP_FIXED])
        self.sleep_combo.setCurrentText(SLEEP_FIXED)
        self.sleep_combo.setEnabled(False)
        form.addRow("休眠チャンネル", self.sleep_combo)

        self.normal_combo = QtWidgets.QComboBox()
        self.normal_combo.addItems(NORMAL_CHOICES)
        form.addRow("通常チャンネル", self.normal_combo)

        self.ai_level2 = QtWidgets.QComboBox()
        self.ai_level2.addItems(AI_CHOICES)
        form.addRow("AI LV2", self.ai_level2)

        self.ai_level3 = QtWidgets.QComboBox()
        self.ai_level3.addItems(AI_CHOICES)
        form.addRow("AI LV3", self.ai_level3)

        self.ai_level4 = QtWidgets.QComboBox()
        self.ai_level4.addItems(AI_CHOICES)
        form.addRow("AI LV4", self.ai_level4)

        layout.addLayout(form)

        layout.addWidget(QtWidgets.QLabel("休眠時間帯"))
        self.sleep_table = QtWidgets.QTableWidget(1, 2)
        self.sleep_table.setHorizontalHeaderLabels(["開始", "終了"])
        self.sleep_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        # 休眠時間帯：1行のみ見える高さに固定
        self.sleep_table.verticalHeader().setVisible(False)
        self.sleep_table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.sleep_table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.sleep_table.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.sleep_table.setFixedHeight(
            self.sleep_table.horizontalHeader().height() + self.sleep_table.rowHeight(0) + 6
        )
        layout.addWidget(self.sleep_table)

        self.timer_table = QtWidgets.QTableWidget(0, 3)
        self.timer_table.setHorizontalHeaderLabels(["開始", "終了", "CH"])
        self.timer_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        delegate = TimeNormalizeDelegate(self.timer_table, self.timer_table)
        self.timer_table.setItemDelegateForColumn(0, delegate)
        self.timer_table.setItemDelegateForColumn(1, delegate)
        # タイマー設定：10行程度見える高さを確保
        self.timer_table.verticalHeader().setVisible(False)
        self.timer_table.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        row_h = self.timer_table.verticalHeader().defaultSectionSize()
        if row_h <= 0:
            row_h = 24
        visible_rows = 10
        self.timer_table.setMinimumHeight(
            self.timer_table.horizontalHeader().height() + row_h * visible_rows + 8
        )
        layout.addWidget(QtWidgets.QLabel("タイマー設定"))
        layout.addWidget(self.timer_table)

        buttons_layout = QtWidgets.QHBoxLayout()
        add_button = QtWidgets.QPushButton("追加")
        remove_button = QtWidgets.QPushButton("削除")
        buttons_layout.addWidget(add_button)
        buttons_layout.addWidget(remove_button)
        layout.addLayout(buttons_layout)

        add_button.clicked.connect(
            lambda: self.add_timer_rule({"start": "00:00", "end": "00:00", "channel": TIMER_CHOICES[0]})
        )
        remove_button.clicked.connect(self.remove_selected_rule)

        action_layout = QtWidgets.QHBoxLayout()
        save_button = QtWidgets.QPushButton("保存")
        cancel_button = QtWidgets.QPushButton("キャンセル")
        self.btn_ref_other = QtWidgets.QPushButton("他チャンネルの設定を参照")
        self.btn_ref_other.setFixedHeight(34)
        action_layout.addStretch()
        action_layout.addWidget(self.btn_ref_other)
        action_layout.addWidget(save_button)
        action_layout.addWidget(cancel_button)
        layout.addLayout(action_layout)

        self._built_config = None
        self.btn_ref_other.clicked.connect(self.on_ref_other_clicked)
        save_button.clicked.connect(self._on_save)
        cancel_button.clicked.connect(self.reject)
        self._reload_form_from_config()

    def _ai_choice_to_display(self, value: Optional[str]) -> str:
        if value == "same_as_normal":
            return AI_CHOICES[0]
        if value in AI_CHOICES:
            return value
        return AI_CHOICES[1]

    def _set_sleep_rule(self, rules: List[dict]) -> None:
        default_rule = {"start": "00:00", "end": "05:00"}
        rule = rules[0] if rules else default_rule
        self.sleep_table.setItem(0, 0, QtWidgets.QTableWidgetItem(rule.get("start", "")))
        self.sleep_table.setItem(0, 1, QtWidgets.QTableWidgetItem(rule.get("end", "")))

    def _rebuild_timer_rows(self, rules: List[dict]) -> None:
        while self.timer_table.rowCount() > 0:
            self.timer_table.removeRow(0)
        for rule in rules:
            self.add_timer_rule(rule)

    def _reload_form_from_config(self) -> None:
        normal_value = self.config.get("normal_channel", "ch05")
        if normal_value not in NORMAL_CHOICES:
            normal_value = NORMAL_CHOICES[0]
        self.normal_combo.setCurrentText(normal_value)
        ai_channels = self.config.get("ai_channels", {})
        self.ai_level2.setCurrentText(self._ai_choice_to_display(ai_channels.get("level2")))
        self.ai_level3.setCurrentText(self._ai_choice_to_display(ai_channels.get("level3")))
        self.ai_level4.setCurrentText(self._ai_choice_to_display(ai_channels.get("level4")))
        self._set_sleep_rule(self.config.get("sleep_rules", []))
        rules = self.config.get("timer_rules", [])
        self._rebuild_timer_rows(rules)

    def add_timer_rule(self, rule: dict) -> None:
        row = self.timer_table.rowCount()
        self.timer_table.insertRow(row)
        self.timer_table.setItem(row, 0, QtWidgets.QTableWidgetItem(rule.get("start", "")))
        self.timer_table.setItem(row, 1, QtWidgets.QTableWidgetItem(rule.get("end", "")))
        channel_combo = QtWidgets.QComboBox()
        channel_combo.addItems(TIMER_CHOICES)
        channel_value = rule.get("channel", TIMER_CHOICES[0])
        if channel_value not in TIMER_CHOICES:
            channel_value = TIMER_CHOICES[0]
        channel_combo.setCurrentText(channel_value)
        self.timer_table.setCellWidget(row, 2, channel_combo)

    def remove_selected_rule(self) -> None:
        row = self.timer_table.currentRow()
        if row >= 0:
            self.timer_table.removeRow(row)

    def _on_save(self) -> None:
        try:
            self._built_config = self.build_config()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "入力エラー", str(exc))
            return
        self.accept()

    def on_ref_other_clicked(self) -> None:
        if self.controller_window is None:
            QtWidgets.QMessageBox.critical(self, "エラー", "参照元一覧を取得できません")
            return

        current_sign = self.sign_name
        candidates = []
        for state in self.controller_window.sign_states.values():
            if not state.exists:
                continue
            if state.name == current_sign:
                continue
            candidates.append(state.name)

        if not candidates:
            QtWidgets.QMessageBox.information(self, "確認", "参照できる対象がありません")
            return

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("他チャンネルの設定を参照")
        layout = QtWidgets.QVBoxLayout(dlg)
        layout.addWidget(QtWidgets.QLabel("参照元サイネージ番号を選択してください（1つ）"))
        listw = QtWidgets.QListWidget()
        listw.addItems(candidates)
        listw.setCurrentRow(0)
        layout.addWidget(listw)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        layout.addWidget(btns)

        def ok():
            item = listw.currentItem()
            if not item:
                return
            src_sign = item.text()
            dlg.accept()
            self._apply_reference_from(src_sign)

        btns.accepted.connect(ok)
        btns.rejected.connect(dlg.reject)
        dlg.resize(360, 420)
        dlg.exec()

    def _apply_reference_from(self, src_sign: str) -> None:
        try:
            src_cfg = read_config(CONFIG_DIR / src_sign)
            self.config = src_cfg
            if "timer_rules" in self.config and isinstance(self.config["timer_rules"], list):
                filtered = []
                for rule in self.config["timer_rules"]:
                    channel = (rule or {}).get("channel", "")
                    if channel == EMERGENCY_CHANNEL:
                        continue
                    filtered.append(rule)
                self.config["timer_rules"] = filtered
            self._reload_form_from_config()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "エラー", f"参照に失敗しました: {exc}")

    def get_config(self) -> Optional[dict]:
        return self._built_config

    def build_config(self) -> dict:
        timer_rules = []
        for row in range(self.timer_table.rowCount()):
            start_item = self.timer_table.item(row, 0)
            end_item = self.timer_table.item(row, 1)
            channel_combo = self.timer_table.cellWidget(row, 2)
            start_text = start_item.text().strip() if start_item else ""
            end_text = end_item.text().strip() if end_item else ""
            channel_text = channel_combo.currentText().strip() if channel_combo else ""
            if not start_text and not end_text and not channel_text:
                continue
            if not start_text or not end_text:
                raise ValueError("タイマー設定の時刻を入力してください")
            timer_rules.append(
                {
                    "start": normalize_hhmm(start_text),
                    "end": normalize_hhmm(end_text),
                    "channel": channel_text,
                }
            )
        start_item = self.sleep_table.item(0, 0)
        end_item = self.sleep_table.item(0, 1)
        sleep_start = normalize_hhmm(start_item.text() if start_item else "")
        sleep_end = normalize_hhmm(end_item.text() if end_item else "")
        sleep_rules = [{"start": sleep_start, "end": sleep_end}]
        return {
            "sleep_channel": SLEEP_FIXED,
            "sleep_rules": sleep_rules,
            "normal_channel": self.normal_combo.currentText(),
            "ai_channels": {
                "level2": self._ai_choice_to_value(self.ai_level2.currentText()),
                "level3": self._ai_choice_to_value(self.ai_level3.currentText()),
                "level4": self._ai_choice_to_value(self.ai_level4.currentText()),
            },
            "timer_rules": timer_rules,
        }

    def _ai_choice_to_value(self, value: str) -> str:
        if value == AI_CHOICES[0]:
            return "same_as_normal"
        return value


class EmittingStream(QtCore.QObject):
    text_written = QtCore.Signal(str)

    def __init__(self, fallback=None):
        super().__init__()
        self._fallback = fallback

    def write(self, text):
        if text:
            self.text_written.emit(text)
            try:
                if self._fallback:
                    self._fallback.write(text)
                    self._fallback.flush()
            except Exception:
                pass

    def flush(self):
        try:
            if self._fallback:
                self._fallback.flush()
        except Exception:
            pass


class LogHandler(logging.Handler):
    def __init__(self, stream: EmittingStream):
        super().__init__()
        self.stream = stream

    def emit(self, record):
        msg = self.format(record)
        self.stream.write(msg + "\n")


class TimerLegendWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(220)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor("white"))
        painter.setPen(QtGui.QPen(QtGui.QColor(80, 80, 80)))
        height = self.height()
        width = self.width()
        right_pad = 40
        label_text = "タイマー設定"
        painter.save()
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        metrics = QtGui.QFontMetrics(font)
        text_width = metrics.horizontalAdvance(label_text)
        shift_left = metrics.horizontalAdvance("あ") * 2
        text_x = width - right_pad - 14 - shift_left
        text_y = int(height / 2 + text_width / 2)
        painter.translate(text_x, text_y)
        painter.rotate(-90)
        painter.drawText(0, 0, label_text)
        painter.restore()
        tick_x2 = width - 2
        tick_x1 = width - 22
        for hour in [0, 6, 12, 18, 23]:
            y = int(height * (hour * 60) / (24 * 60))
            painter.drawLine(tick_x1, y, tick_x2, y)
            painter.drawText(
                QtCore.QRect(0, y - 8, width - right_pad, 16),
                QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                f"{hour:02d}:00",
            )

        legend_item_height = 16
        legend_height = len(TIMER_CHANNEL_COLORS) * legend_item_height
        legend_top = max(8, height - legend_height - 20)
        x = 6
        y = legend_top
        for channel, color in TIMER_CHANNEL_COLORS.items():
            painter.fillRect(x, y, 12, 12, color)
            painter.drawRect(x, y, 12, 12)
            painter.drawText(x + 18, y + 11, channel)
            y += legend_item_height


class TimerBarWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rules = []
        self._sleep_rules = []
        self._enabled = True

    def set_rules(self, rules: List[dict]) -> None:
        self._rules = rules
        self.update()

    def set_sleep_rules(self, rules: List[dict]) -> None:
        self._sleep_rules = rules or []
        self.update()

    def set_column_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        background = QtGui.QColor(245, 245, 245) if not self._enabled else QtGui.QColor("white")
        painter.fillRect(self.rect(), background)
        height = self.height()
        width = self.width()
        painter.setPen(QtGui.QPen(QtGui.QColor(220, 220, 220)))
        for hour in range(0, 25, 2):
            y = int(height * (hour * 60) / (24 * 60))
            painter.drawLine(0, y, width, y)

        # 休眠帯（黒っぽいねずみ色）を背景として表示
        sleep_color = QtGui.QColor(90, 90, 90)
        sleep_color.setAlpha(140)
        for rule in self._sleep_rules:
            try:
                start = parse_time(rule["start"])
                end = parse_time(rule["end"])
            except Exception:
                continue
            start_minutes = start.hour * 60 + start.minute
            end_minutes = end.hour * 60 + end.minute
            if start_minutes == end_minutes:
                continue
            if start_minutes < end_minutes:
                self._paint_segment(painter, start_minutes, end_minutes, sleep_color, height, width)
            else:
                self._paint_segment(painter, start_minutes, 24 * 60, sleep_color, height, width)
                self._paint_segment(painter, 0, end_minutes, sleep_color, height, width)

        for rule in self._rules:
            try:
                start = parse_time(rule["start"])
                end = parse_time(rule["end"])
            except Exception:
                continue
            color = TIMER_CHANNEL_COLORS.get(rule.get("channel"), QtGui.QColor(200, 200, 200))
            start_minutes = start.hour * 60 + start.minute
            end_minutes = end.hour * 60 + end.minute
            if start_minutes == end_minutes:
                continue
            if start_minutes < end_minutes:
                self._paint_segment(painter, start_minutes, end_minutes, color, height, width)
            else:
                self._paint_segment(painter, start_minutes, 24 * 60, color, height, width)
                self._paint_segment(painter, 0, end_minutes, color, height, width)

    def _paint_segment(self, painter, start_minutes, end_minutes, color, height, width):
        y1 = int(height * start_minutes / (24 * 60))
        y2 = int(height * end_minutes / (24 * 60))
        painter.fillRect(0, y1, width, max(1, y2 - y1), color)


class SignageColumnWidget(QtWidgets.QWidget):
    clicked_config = QtCore.Signal(str)
    clicked_reboot = QtCore.Signal(str)
    clicked_shutdown = QtCore.Signal(str)
    toggled_active = QtCore.Signal(str, bool)

    def __init__(self, name: str, sign_id: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.sign_id = sign_id
        self.layout = QtWidgets.QVBoxLayout(self)
        self.layout.setContentsMargins(4, 2, 4, 2)
        self.layout.setSpacing(2)

        self.display_label = self._make_label("-")
        self.preview_widget = QtWidgets.QWidget()
        self.preview_widget.setFixedHeight(105)
        preview_layout = QtWidgets.QStackedLayout(self.preview_widget)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_label = QtWidgets.QLabel("サンプルなし")
        self.preview_label.setAlignment(QtCore.Qt.AlignCenter)
        preview_layout.addWidget(self.preview_label)
        self.video_widget = None
        self.player = None
        self._current_sample: Optional[Path] = None
        self.sample_list: List[Path] = []
        self.sample_index = 0
        self.current_channel: Optional[str] = None
        if HAS_QTMULTIMEDIA:
            self.video_widget = QtMultimediaWidgets.QVideoWidget()
            preview_layout.addWidget(self.video_widget)
            self.player = QtMultimedia.QMediaPlayer()
            self.player.setVideoOutput(self.video_widget)
            self.player.mediaStatusChanged.connect(self._handle_media_status)
        self.preview_stack = preview_layout
        self.setting_button = QtWidgets.QPushButton("変更")
        self.sleep_label = self._make_label("-")
        self.ai_lv2_label = self._make_label("-")
        self.ai_lv3_label = self._make_label("-")
        self.ai_lv4_label = self._make_label("-")
        self.normal_label = self._make_label("-")
        self.timer_bar = TimerBarWidget()
        self.timer_bar.setFixedHeight(220)

        self.power_widget = QtWidgets.QWidget()
        power_layout = QtWidgets.QVBoxLayout(self.power_widget)
        power_layout.setContentsMargins(0, 0, 0, 0)
        power_layout.setSpacing(0)
        self.btn_reboot = QtWidgets.QPushButton("再起動")
        self.btn_shutdown = QtWidgets.QPushButton("シャットダウン")
        font = self.btn_shutdown.font()
        font.setPointSize(max(8, font.pointSize() - 1))
        self.btn_shutdown.setFont(font)
        self.btn_reboot.setFixedHeight(26)
        self.btn_shutdown.setFixedHeight(26)
        power_layout.addWidget(self.btn_reboot)
        power_layout.addWidget(self.btn_shutdown)

        self.manage_widget = QtWidgets.QWidget()
        manage_layout = QtWidgets.QVBoxLayout(self.manage_widget)
        manage_layout.setContentsMargins(0, 0, 0, 0)
        manage_layout.setSpacing(0)
        self.btn_active = QtWidgets.QPushButton("アクティブ")
        self.btn_active.setCheckable(True)
        self.btn_active.setFixedHeight(26)
        self.comm_label = QtWidgets.QLabel("通信--")
        self.comm_label.setAlignment(QtCore.Qt.AlignCenter)
        self.comm_label.setFixedHeight(26)
        manage_layout.addWidget(self.btn_active)
        manage_layout.addWidget(self.comm_label)

        self.pc_status_labels: Dict[str, QtWidgets.QLabel] = {}
        for key, _ in PC_STATUS_ITEMS:
            label = self._make_pc_status_label("-")
            self.pc_status_labels[key] = label

        for widget, height in [
            (self.display_label, 28),
            (self.preview_widget, 105),
            (self.setting_button, 26),
            (self.sleep_label, 20),
            (self.ai_lv2_label, 20),
            (self.ai_lv3_label, 20),
            (self.ai_lv4_label, 20),
            (self.normal_label, 20),
            (self.timer_bar, 220),
            (self.power_widget, 52),
            (self.manage_widget, 52),
        ]:
            widget.setFixedHeight(height)
            self.layout.addWidget(widget)

        for key, _ in PC_STATUS_ITEMS:
            self.layout.addWidget(self.pc_status_labels[key])

        self._apply_3d_button_style(self.setting_button)
        self._apply_3d_button_style(self.btn_reboot)
        self._apply_3d_button_style(self.btn_shutdown)

        self.setting_button.clicked.connect(
            lambda: self.clicked_config.emit(self.sign_id)
        )
        self.btn_reboot.clicked.connect(
            lambda: self.clicked_reboot.emit(self.sign_id)
        )
        self.btn_shutdown.clicked.connect(
            lambda: self.clicked_shutdown.emit(self.sign_id)
        )
        self.btn_active.toggled.connect(
            lambda checked: self.toggled_active.emit(self.sign_id, checked)
        )
        self.set_comm_status(True, None)

    def _apply_3d_button_style(self, btn: QtWidgets.QPushButton) -> None:
        btn.setStyleSheet(
            """
QPushButton {
  padding: 4px 6px;
  border: 1px solid #8a8a8a;
  border-radius: 6px;
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                              stop:0 #ffffff, stop:1 #e6e6e6);
}
QPushButton:pressed {
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                              stop:0 #dcdcdc, stop:1 #f6f6f6);
}
"""
        )

    def _make_label(self, text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet("border: 1px solid #999;")
        return label

    def _make_pc_status_label(self, text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setWordWrap(True)
        label.setFixedHeight(PC_STATUS_ROW_HEIGHT)
        font = label.font()
        font.setPointSize(PC_STATUS_FONT_SIZE)
        label.setFont(font)
        label.setContentsMargins(0, 0, 0, 0)
        label.setStyleSheet("border: 1px solid #999;")
        return label

    def set_pc_status_values(self, values: Dict[str, str]) -> None:
        for key, label in self.pc_status_labels.items():
            label.setText(values.get(key, "-"))

    def _handle_media_status(self, status) -> None:
        if status != QtMultimedia.QMediaPlayer.MediaStatus.EndOfMedia:
            return
        self._advance_sample()

    def _advance_sample(self) -> None:
        if not self.sample_list:
            return
        self.sample_index = (self.sample_index + 1) % len(self.sample_list)
        self._play_current_sample()

    def show_preview_message(self, text: str) -> None:
        self.preview_label.setText(text)
        self.preview_label.setPixmap(QtGui.QPixmap())
        self.preview_stack.setCurrentWidget(self.preview_label)
        if self.player:
            self.player.stop()
        self._current_sample = None

    def show_preview_pixmap(self, pixmap: QtGui.QPixmap) -> None:
        self.preview_label.setText("")
        self.preview_label.setPixmap(pixmap)
        self.preview_stack.setCurrentWidget(self.preview_label)
        if self.player:
            self.player.stop()
        self._current_sample = None

    def set_sample_list(self, samples: List[Path]) -> None:
        if samples != self.sample_list:
            self.sample_list = samples
            self.sample_index = 0
            self._current_sample = None
        if not self.sample_list:
            self.show_preview_message("サンプルなし")
            return
        self._play_current_sample()

    def _play_current_sample(self) -> None:
        if not self.sample_list:
            return
        self.play_preview(self.sample_list[self.sample_index])

    def play_preview(self, sample: Path) -> None:
        if not self.player or not self.video_widget:
            self.show_preview_message(sample.name)
            return
        if self._current_sample != sample:
            self.player.setSource(QtCore.QUrl.fromLocalFile(str(sample)))
            self._current_sample = sample
        self.preview_stack.setCurrentWidget(self.video_widget)
        self.player.play()

    def set_active_state(self, active: bool) -> None:
        label = "アクティブ" if active else "非アクティブ"
        blocker = QtCore.QSignalBlocker(self.btn_active)
        self.btn_active.setText(label)
        self.btn_active.setChecked(active)
        del blocker
        if active:
            self.btn_active.setStyleSheet("background:#e8ffe8; border:2px solid #2e7d32; font-weight:800;")
        else:
            self.btn_active.setStyleSheet("background:#c9c9c9; border:2px solid #7a7a7a; font-weight:700;")

    def set_inactive_style(self, inactive: bool) -> None:
        if inactive:
            self.setStyleSheet("background-color: #c9c9c9; color: #7a7a7a;")
        else:
            self.setStyleSheet("")
        self.timer_bar.set_column_enabled(not inactive)

    def set_comm_status(self, enabled: bool, online: Optional[bool]) -> None:
        if not enabled:
            self.comm_label.setText("-")
            self.comm_label.setStyleSheet("background:#c9c9c9; color:#333; border-radius:6px;")
            return
        if online is None:
            self.comm_label.setText("通信--")
            self.comm_label.setStyleSheet("background:#eeeeee; color:#333; border-radius:6px;")
            return
        if online:
            self.comm_label.setText("通信OK")
            self.comm_label.setStyleSheet(
                "background:#2d7ff9; color:#fff; border-radius:6px; font-weight:800;"
            )
        else:
            self.comm_label.setText("通信NG")
            self.comm_label.setStyleSheet(
                "background:#e53935; color:#fff; border-radius:6px; font-weight:800;"
            )


class ControllerWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1400, 900)

        self.settings = load_json(SETTINGS_PATH, {})
        self.inventory = load_json(INVENTORY_PATH, {})
        self.ai_status = load_json(AI_STATUS_PATH, {})

        self.sign_states: Dict[str, SignState] = {}
        self._preview_enabled = self.settings.get("preview_enabled", True)
        self._executor = ThreadPoolExecutor(max_workers=self.settings.get("thread_workers", 8))
        self._update_lock = threading.Lock()
        self._observer = None
        self._ai_status_mtime: Optional[float] = None
        self._log_stream = None
        self._log_handler = None
        self._last_log_text = ""
        self._last_log_count = 0
        self._log_buffer = ""
        self._header_labels: Dict[str, QtWidgets.QPushButton] = {}
        self._column_widgets: Dict[str, SignageColumnWidget] = {}
        self.ai_level_badge: Optional[QtWidgets.QLabel] = None
        self.left_panel: Optional[QtWidgets.QWidget] = None
        self.header_buttons: List[QtWidgets.QPushButton] = []
        self.columns: List[SignageColumnWidget] = []
        self._emergency_override_enabled: bool = False
        self._emergency_override_channel: str = EMERGENCY_CHANNEL
        self._remote_status_cache: Dict[str, dict] = {}
        self._remote_status_pending: Dict[str, dict] = {}
        self._remote_status_log_state: Dict[str, str] = {}
        self._ui_busy: bool = False
        self._busy_label: str = ""
        # ---- Debug trace (root cause investigation) ----
        self._dbg_enabled = bool(self.settings.get("debug_trace_enabled", True))
        self._dbg_hang_seconds = int(self.settings.get("debug_hang_seconds", 60))
        self._dbg_last_progress = {"op_id": "", "title": "", "token": "", "ts": 0.0}
        self._dbg_ui_post_seq = 0
        self._dbg_ui_post_pending = {}  # seq -> {"label": str, "ts": float}
        self._dbg_watchdog_timer: Optional[QtCore.QTimer] = None
        self._op_seq = 0
        self._op_lines: Dict[str, int] = {}
        self._op_results: Dict[str, dict] = {}
        self._op_done_labels: Dict[str, str] = {}
        self._distribute_busy = False
        self._pc_status_skip_until: Dict[str, float] = {}
        # ---- Telemetry軽量化用 ----
        self._telemetry_rr_index = 0  # round-robin index
        self._telemetry_batch_size = int(self.settings.get("telemetry_batch_size", 5))  # 1回に更新する台数
        self._telemetry_min_interval_ok = float(self.settings.get("telemetry_min_interval_ok", 10.0))
        self._telemetry_min_interval_ng = float(self.settings.get("telemetry_min_interval_ng", 30.0))
        self._telemetry_max_interval_ng = float(self.settings.get("telemetry_max_interval_ng", 120.0))
        # per-sign backoff state: { "next_allowed": monotonic_time, "fail_count": int }
        self._telemetry_backoff: Dict[str, dict] = {}
        # per-sign last log state is already self._remote_status_log_state
        self._telemetry_timer: Optional[QtCore.QTimer] = None
        self._connectivity_timer: Optional[QtCore.QTimer] = None

        self._init_ui()
        QtCore.QTimer.singleShot(0, self.apply_dynamic_column_widths)
        self._setup_log_stream()
        self._load_sign_states()
        self.refresh_summary()
        self.start_watchers()

        self.timer_poll = QtCore.QTimer(self)
        self.timer_poll.setInterval(60 * 1000)
        self.timer_poll.timeout.connect(self.check_timer_transition)
        self.timer_poll.start()

        self._telemetry_timer = QtCore.QTimer(self)
        self._telemetry_timer.setInterval(10000)
        self._telemetry_timer.timeout.connect(self.refresh_remote_telemetry)
        self._telemetry_timer.start()
        self.refresh_remote_telemetry()

        self._connectivity_timer = QtCore.QTimer(self)
        self._connectivity_timer.setInterval(10000)
        self._connectivity_timer.timeout.connect(self.poll_connectivity_silent)
        self._connectivity_timer.start()

        # watchdog: UI busy が一定時間続く場合にスレッドダンプをログ出力（挙動変更なし）
        self._dbg_watchdog_timer = QtCore.QTimer(self)
        self._dbg_watchdog_timer.setInterval(1000)  # 1秒周期
        self._dbg_watchdog_timer.timeout.connect(self._dbg_watchdog_tick)
        self._dbg_watchdog_timer.start()

    def _init_ui(self) -> None:
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(OUTER_MARGIN, OUTER_MARGIN, OUTER_MARGIN, OUTER_MARGIN)
        layout.setSpacing(3)

        header_layout = QtWidgets.QHBoxLayout()
        title_label = QtWidgets.QLabel("津山駅 SuperAI Signage System Controller")
        title_font = title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_font.setItalic(True)
        title_label.setFont(title_font)
        header_layout.addWidget(title_label)

        button_layout = QtWidgets.QHBoxLayout()
        self.btn_check = QtWidgets.QPushButton("サイネージPC一斉通信確認")
        self.btn_bulk_update = QtWidgets.QPushButton("一斉Ch更新")
        self.btn_refresh_content = QtWidgets.QPushButton("このPCの動画情報読込み")
        self.btn_sync = QtWidgets.QPushButton("このPCの動画情報を全サイネージPCへ（同期）")
        self.btn_logs = QtWidgets.QPushButton("全サイネージPCのLOGファイル取得")
        self.btn_preview_toggle = QtWidgets.QPushButton("プレビューON/OFF")

        for btn in [
            self.btn_check,
            self.btn_bulk_update,
            self.btn_refresh_content,
            self.btn_sync,
            self.btn_logs,
            self.btn_preview_toggle,
        ]:
            self._apply_3d_button_style(btn)
            button_layout.addWidget(btn)

        header_layout.addStretch()
        header_layout.addLayout(button_layout)
        # --- 最上位強制メッセージ（20ch） ---
        self.btn_emergency_override = QtWidgets.QPushButton("最上位強制メッセージ（20ch）")
        self.btn_emergency_override.setCheckable(True)
        self.btn_emergency_override.setFixedHeight(44)
        self.btn_emergency_override.setMinimumWidth(220)
        self._apply_3d_button_style(self.btn_emergency_override)
        header_layout.addWidget(self.btn_emergency_override)
        self.ai_level_badge = QtWidgets.QLabel("渋滞LEVEL1")
        self.ai_level_badge.setAlignment(QtCore.Qt.AlignCenter)
        self.ai_level_badge.setFixedSize(160, 44)
        self.ai_level_badge.setStyleSheet("border-radius:8px; font-weight:900; font-size:16px;")
        header_layout.addWidget(self.ai_level_badge)
        layout.addLayout(header_layout)

        signage_grid_container = QtWidgets.QWidget()
        signage_grid = QtWidgets.QGridLayout(signage_grid_container)
        signage_grid.setContentsMargins(0, 0, 0, 0)
        signage_grid.setHorizontalSpacing(GAP_PX)
        signage_grid.setVerticalSpacing(3)

        left_header = QtWidgets.QLabel("")
        left_header.setFixedSize(LEFT_COL_WIDTH, 42)
        signage_grid.addWidget(left_header, 0, 0)

        self.header_buttons = []
        for idx in range(N_SIGNAGE):
            name = f"Signage {idx + 1:02d}"
            button = QtWidgets.QPushButton(name)
            button.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
            button.setFixedHeight(42)
            button.setStyleSheet("border: 1px solid #999;")
            signage_grid.addWidget(button, 0, BASE_COL + idx)
            self.header_buttons.append(button)
            self._header_labels[name.replace("Signage ", "Signage")] = button

        self.left_panel = QtWidgets.QWidget()
        self.left_panel.setFixedWidth(LEFT_COL_WIDTH)
        left_layout = QtWidgets.QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(2, 2, 2, 2)
        left_layout.setSpacing(2)

        left_layout.addWidget(self._make_row_label("表示中ch", 28))
        left_layout.addWidget(self._make_row_label("表示中映像", 105))
        left_layout.addWidget(self._make_row_label("CH設定→", 26))
        left_layout.addWidget(self._make_row_label("休眠時", 20))
        left_layout.addWidget(self._make_row_label("AI渋滞判定(LV2)時", 20))
        left_layout.addWidget(self._make_row_label("AI渋滞判定(LV3)時", 20))
        left_layout.addWidget(self._make_row_label("AI渋滞判定(LV4)時", 20))
        left_layout.addWidget(self._make_row_label("通常時", 20))
        timer_label = TimerLegendWidget()
        timer_label.setFixedHeight(220)
        left_layout.addWidget(timer_label)
        power_label = self._make_row_label("サイネージPC\n電源管理", 52)
        power_label.setStyleSheet("border: 1px solid #999; color: #d32f2f; font-weight: 900;")
        left_layout.addWidget(power_label)
        left_layout.addWidget(self._make_row_label("管理する\nサイネージ", 52))
        for _, label_text in PC_STATUS_ITEMS:
            left_layout.addWidget(self._make_pc_status_row_label(label_text))

        signage_grid.addWidget(self.left_panel, 1, 0)

        self.columns = []
        for idx in range(N_SIGNAGE):
            name = f"Signage{idx + 1:02d}"
            sign_id = f"Sign{idx + 1:02d}"
            column = SignageColumnWidget(name, sign_id)
            column.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
            column.setMinimumWidth(0)
            self.columns.append(column)
            signage_grid.addWidget(column, 1, BASE_COL + idx)
            self._column_widgets[name] = column
            column.clicked_config.connect(self._on_column_config)
            column.clicked_reboot.connect(self._on_column_reboot)
            column.clicked_shutdown.connect(self._on_column_shutdown)
            column.toggled_active.connect(self._on_column_active_toggle)

        signage_grid.setColumnMinimumWidth(0, LEFT_COL_WIDTH)
        for idx in range(N_SIGNAGE):
            signage_grid.setColumnStretch(BASE_COL + idx, 1)

        layout.addWidget(signage_grid_container)

        log_label = QtWidgets.QLabel("ログ")
        layout.addWidget(log_label)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setStyleSheet("background-color: #000; color: #fff;")
        log_font = self.log_view.font()
        log_font.setPointSize(9)
        self.log_view.setFont(log_font)
        self.log_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.log_view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.log_view.setFixedHeight(135)
        layout.addWidget(self.log_view)

        self.setCentralWidget(central)

        self.btn_check.clicked.connect(self.check_connectivity)
        self.btn_bulk_update.clicked.connect(self.bulk_update)
        self.btn_refresh_content.clicked.connect(self.refresh_content_request)
        self.btn_sync.clicked.connect(self.start_sync)
        self.btn_logs.clicked.connect(self.collect_logs)
        self.btn_preview_toggle.clicked.connect(self.toggle_preview)
        self.btn_emergency_override.toggled.connect(self.toggle_emergency_override)
        self._apply_emergency_button_style()

        QtCore.QTimer.singleShot(800, self.check_connectivity)

    def apply_dynamic_column_widths(self) -> None:
        total_w = self.centralWidget().width()
        usable = total_w - LEFT_COL_WIDTH - OUTER_MARGIN * 2 - GAP_PX * N_SIGNAGE
        col_w = max(45, int(usable / N_SIGNAGE))

        if self.left_panel:
            self.left_panel.setFixedWidth(LEFT_COL_WIDTH)

        if self.header_buttons:
            for button in self.header_buttons:
                button.setFixedWidth(col_w)

        if self.columns:
            for column in self.columns:
                column.setFixedWidth(col_w)
                column.setMinimumWidth(0)
                column.setMaximumWidth(col_w)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QtCore.QTimer.singleShot(0, self.apply_dynamic_column_widths)

    def _make_row_label(self, text: str, height: int) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setFixedHeight(height)
        label.setStyleSheet("border: 1px solid #999;")
        label.setWordWrap(True)
        return label

    def _apply_3d_button_style(self, btn: QtWidgets.QPushButton) -> None:
        btn.setStyleSheet(
            """
QPushButton {
  padding: 6px 10px;
  border: 1px solid #8a8a8a;
  border-radius: 6px;
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                              stop:0 #ffffff, stop:1 #e6e6e6);
}
QPushButton:hover {
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                              stop:0 #ffffff, stop:1 #f0f0f0);
}
QPushButton:pressed {
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                              stop:0 #dcdcdc, stop:1 #f6f6f6);
  border: 1px solid #6f6f6f;
}
QPushButton:disabled {
  background: #d6d6d6;
  color: #777;
}
"""
        )

    def _apply_emergency_button_style(self) -> None:
        if not hasattr(self, "btn_emergency_override") or self.btn_emergency_override is None:
            return
        self.btn_emergency_override.setStyleSheet(
            """
QPushButton {
  padding: 6px 10px;
  border: 1px solid #8a8a8a;
  border-radius: 8px;
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #e6e6e6);
}
QPushButton:checked {
  background: #e53935;
  color: #fff;
  font-weight: 900;
  font-size: 14px;
  border: 1px solid #7a1f1f;
}
QPushButton:checked:disabled {
  background: #e53935;
  color: #fff;
}
QPushButton:disabled {
  background: #d6d6d6;
  color: #777;
}
"""
        )

    def _make_pc_status_row_label(self, text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setFixedHeight(PC_STATUS_ROW_HEIGHT)
        label.setWordWrap(True)
        font = label.font()
        font.setPointSize(PC_STATUS_FONT_SIZE)
        label.setFont(font)
        label.setContentsMargins(0, 0, 0, 0)
        label.setStyleSheet("border: 1px solid #999;")
        return label

    def _make_chip(self, title: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(f"{title}: -")
        label.setProperty("title", title)
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setFixedHeight(26)
        label.setMinimumWidth(120)
        label.setStyleSheet(
            "background:#ffffff; color:#111; border:1px solid #bbb; border-radius:8px; font-weight:800;"
        )
        return label

    def _make_status_cell(self) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel("-")
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setWordWrap(True)
        font = label.font()
        font.setPointSize(8)
        label.setFont(font)
        label.setStyleSheet(
            "background:#ffffff; color:#111; border:1px solid #bbb; border-radius:8px; padding:2px 4px;"
        )
        return label

    def _chip_set_value(self, label: QtWidgets.QLabel, value: Optional[float], unit: str, kind: str) -> None:
        title = label.property("title") or ""
        if value is None:
            label.setText(f"{title}: -")
            label.setStyleSheet(
                "background:#ffffff; color:#111; border:1px solid #bbb; border-radius:8px; font-weight:800;"
            )
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            label.setText(f"{title}: -")
            label.setStyleSheet(
                "background:#ffffff; color:#111; border:1px solid #bbb; border-radius:8px; font-weight:800;"
            )
            return
        if kind == "load":
            warn_threshold = 70
            danger_threshold = 90
        else:
            warn_threshold = 55
            danger_threshold = 65
        if numeric >= danger_threshold:
            background = "#e53935"
            color = "#fff"
        elif numeric >= warn_threshold:
            background = "#ffd6e7"
            color = "#111"
        else:
            background = "#ffffff"
            color = "#111"
        label.setText(f"{title}: {numeric:.1f}{unit}")
        label.setStyleSheet(
            f"background:{background}; color:{color}; border:1px solid #bbb; border-radius:8px; font-weight:800;"
        )

    def _status_style(self, severity: int) -> Tuple[str, str]:
        if severity >= 2:
            return "#e53935", "#fff"
        if severity == 1:
            return "#ffd6e7", "#111"
        return "#ffffff", "#111"

    def _set_status_label(self, label: QtWidgets.QLabel, text: str, severity: int) -> None:
        background, color = self._status_style(severity)
        label.setText(text)
        label.setStyleSheet(
            f"background:{background}; color:{color}; border:1px solid #bbb; border-radius:8px; padding:2px 4px;"
        )

    def _set_status_error(self, label: QtWidgets.QLabel, text: str) -> None:
        label.setText(text)
        label.setStyleSheet(
            "background:#e53935; color:#fff; border:1px solid #bbb; border-radius:8px; padding:2px 4px;"
        )

    def _set_status_inactive(self, label: QtWidgets.QLabel, text: str) -> None:
        label.setText(text)
        label.setStyleSheet(
            "background:#c9c9c9; color:#666; border:1px solid #bbb; border-radius:8px; padding:2px 4px;"
        )

    def _calc_severity(self, value: Optional[float], kind: str) -> Optional[int]:
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if kind == "load":
            warn_threshold = 70
            danger_threshold = 90
        else:
            warn_threshold = 55
            danger_threshold = 65
        if numeric >= danger_threshold:
            return 2
        if numeric >= warn_threshold:
            return 1
        return 0

    def _format_metric(self, value: Optional[float], unit: str, decimals: int = 1) -> str:
        if value is None:
            return "不明"
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "不明"
        return f"{numeric:.{decimals}f}{unit}"

    def _set_ssd_usage_label(self, label: QtWidgets.QLabel, used_gb: Optional[float], total_gb: Optional[float]) -> None:
        if used_gb is None or total_gb in (None, 0):
            label.setText("SSD使用状況 不明")
            label.setStyleSheet(
                "background:#ffffff; color:#111; border:1px solid #bbb; border-radius:8px; padding:2px 8px;"
            )
            return
        try:
            usage_percent = (float(used_gb) / float(total_gb)) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            self._set_status_error(label, "SSD使用状況 エラー")
            return
        if usage_percent >= 90:
            severity = 2
        elif usage_percent >= 80:
            severity = 1
        else:
            severity = 0
        background, color = self._status_style(severity)
        label.setText(f"SSD使用状況 {float(used_gb):.1f}GB/{float(total_gb):.0f}GB")
        label.setStyleSheet(
            f"background:{background}; color:{color}; border:1px solid #bbb; border-radius:8px; padding:2px 8px;"
        )

    def _format_pc_value(self, value: Optional[float], decimals: int = 1) -> str:
        if value is None:
            return "不明"
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "不明"
        return f"{numeric:.{decimals}f}"

    def _format_pc_timestamp(self, value: Optional[str]) -> str:
        if not value:
            return "不明"
        try:
            parsed = datetime.fromisoformat(value)
        except Exception:
            return "不明"
        return parsed.strftime("%m/%d %H:%M:%S")

    def _build_pc_status_values(self, payload: Optional[dict]) -> Dict[str, str]:
        values = {key: "不明" for key, _ in PC_STATUS_ITEMS}
        if not isinstance(payload, dict):
            return values

        cpu_load = payload.get("cpu_total_percent")
        mem_used = payload.get("mem_used_percent")
        used_gb = payload.get("ssd", {}).get("used_gb")
        total_gb = payload.get("ssd", {}).get("total_gb")

        auto_play = payload.get("auto_play", {}) if isinstance(payload.get("auto_play"), dict) else {}
        player = payload.get("player", {}) if isinstance(payload.get("player"), dict) else {}
        running = auto_play.get("running")
        alive = player.get("alive")
        if running is True:
            playback_state = "再生中" if alive is True else "固まり疑い"
        elif running is False:
            playback_state = "停止"
        else:
            playback_state = "不明"

        values["last_update"] = self._format_pc_timestamp(payload.get("timestamp"))
        values["playback_state"] = playback_state
        values["cpu_usage"] = self._format_pc_value(cpu_load)
        values["mem_usage"] = self._format_pc_value(mem_used)
        if used_gb is not None and total_gb not in (None, 0):
            try:
                used_i = int(round(float(used_gb)))
                total_i = int(round(float(total_gb)))
                values["c_drive"] = f"{used_i:03d}GB/{total_i:03d}GB"
            except (TypeError, ValueError):
                values["c_drive"] = "-"
        return values

    def _set_pc_status_values(self, state: SignState, payload: Optional[dict]) -> None:
        column = self._column_widgets.get(state.name.replace("Sign", "Signage"))
        if not column:
            return
        values = self._build_pc_status_values(payload)
        column.set_pc_status_values(values)
        playback_label = column.pc_status_labels.get("playback_state")
        if playback_label:
            if values.get("playback_state") == "停止":
                playback_label.setStyleSheet(
                    "background:#e53935; color:#ffffff; border:1px solid #999; font-weight:900;"
                )
            else:
                playback_label.setStyleSheet("border:1px solid #999;")

    def _setup_log_stream(self) -> None:
        def excepthook(exc_type, exc_value, exc_traceback):
            formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
            logging.error("%s", formatted)
            try:
                sys.__stderr__.write(formatted)
                sys.__stderr__.flush()
            except Exception:
                pass

        sys.excepthook = excepthook

    def _remote_status_path(self, state: SignState) -> Path:
        remote_path = build_unc_path(
            state.ip,
            state.share_name,
            f"{REMOTE_LOGS_DIR}\\status\\pc_status.json",
        )
        return Path(remote_path)

    def load_pc_status(self, state: SignState) -> dict:
        path = self._remote_status_path(state)
        try:
            if not path.exists():
                return {"ok": False, "error": "not_found"}
        except Exception:
            return {"ok": False, "error": "not_found"}
        fingerprint = stat_fingerprint(path)
        cached = self._remote_status_cache.get(state.name)
        if cached and cached.get("fingerprint") == fingerprint:
            return {"ok": True, "payload": cached.get("payload"), "cached": True}
        payload = safe_read_json(path, default=None, retries=3)
        if payload is None:
            return {"ok": False, "error": "read_failed"}
        self._remote_status_cache[state.name] = {"fingerprint": fingerprint, "payload": payload}
        return {"ok": True, "payload": payload, "cached": False}

    def refresh_remote_telemetry(self) -> None:
        """
        10秒周期で呼ばれるが、
        - round-robinで一部の端末だけ更新
        - 445が落ちている端末はUNCを触らない
        - NG端末はバックオフで更新頻度を落とす
        """
        now = time_module.monotonic()

        # 更新対象のリスト（exists & enabled のみ）
        targets = [s for s in self.sign_states.values() if s.exists and s.enabled]
        if not targets:
            # 全部無効なら全列の表示を落とす（必要なら）
            for state in self.sign_states.values():
                self._remote_status_pending.pop(state.name, None)
                self._set_pc_status_values(state, None)
            return

        # round-robin 対象を決定
        batch = max(1, self._telemetry_batch_size)
        start = self._telemetry_rr_index % len(targets)
        picked = []
        for i in range(len(targets)):
            idx = (start + i) % len(targets)
            picked.append(targets[idx])
            if len(picked) >= batch:
                break
        self._telemetry_rr_index = (start + batch) % len(targets)

        # timeout（future.cancel はUNC詰まりには効かないので、触る前に落とす）
        timeout_sec = 2.0

        for state in picked:
            skip_until = self._pc_status_skip_until.get(state.name, 0)
            if now < skip_until:
                if not self._fast_smb_reachable(state.ip, timeout_sec=0.2):
                    self._apply_remote_status(state, {"ok": False, "error": "smb_unreachable"})
                continue
            # backoff判定
            meta = self._telemetry_backoff.get(state.name)
            if meta and now < meta.get("next_allowed", 0):
                continue

            # 既にpendingなら結果回収/タイムアウト処理だけ
            pending = self._remote_status_pending.get(state.name)
            if pending:
                future = pending["future"]
                started = pending["started"]
                if future.done():
                    self._remote_status_pending.pop(state.name, None)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"ok": False, "error": str(exc)}
                    self._apply_remote_status(state, result)
                elif now - started > timeout_sec:
                    # cancelしてもUNCが詰まっていると止まらないことがあるので、
                    # “結果は捨てる”扱いでUIを先に進める
                    self._remote_status_pending.pop(state.name, None)
                    self._apply_remote_status(state, {"ok": False, "error": "timeout"})
                continue

            # 445チェックで落とす（ここが最重要）
            if not self._fast_smb_reachable(state.ip, timeout_sec=0.2):
                self._apply_remote_status(state, {"ok": False, "error": "smb_unreachable"})
                continue

            # ここまで来たらUNCを触る（ワーカーへ）
            future = self._executor.submit(self.load_pc_status, state)
            self._remote_status_pending[state.name] = {"future": future, "started": now}

    def _apply_remote_status(self, state: SignState, result: dict) -> None:
        now = time_module.monotonic()

        def set_backoff_fail(reason: str) -> None:
            meta = self._telemetry_backoff.get(state.name, {"fail_count": 0, "next_allowed": 0})
            meta["fail_count"] = int(meta.get("fail_count", 0)) + 1
            base = float(self._telemetry_min_interval_ng)
            maxv = float(self._telemetry_max_interval_ng)
            interval = min(maxv, base * (2 ** max(0, meta["fail_count"] - 1)))
            meta["next_allowed"] = now + interval
            self._telemetry_backoff[state.name] = meta

            # UI更新
            self._set_pc_status_values(state, None)

            # ログはエラーのみ（同一内容は連打しない）
            log_line = f"[ERR] {state.name} pc_status取得失敗 ({reason})"
            if self._remote_status_log_state.get(state.name) != log_line:
                logging.info("%s", log_line)
                self._remote_status_log_state[state.name] = log_line

        def clear_backoff_ok() -> None:
            self._telemetry_backoff[state.name] = {
                "fail_count": 0,
                "next_allowed": now + float(self._telemetry_min_interval_ok),
            }

        if not result.get("ok"):
            error = result.get("error") or "error"
            set_backoff_fail(error)
            return

        payload = result.get("payload")
        if not isinstance(payload, dict):
            set_backoff_fail("payload")
            return

        # OK: UI更新
        self._set_pc_status_values(state, payload)
        clear_backoff_ok()

        # 成功ログは抑制（ログUI負荷対策）
        # 必要なら初回だけ出すなどにしても良いが、基本は出さない
        self._remote_status_log_state[state.name] = "[OK suppressed]"

    def append_log_text(self, text: str) -> None:
        if not text:
            return
        self._log_buffer += text
        while "\n" in self._log_buffer:
            line, self._log_buffer = self._log_buffer.split("\n", 1)
            self._append_log_line(line)

    def _append_log_line(self, line: str) -> None:
        shortened = self._shorten_log_line(line)
        if shortened == self._last_log_text:
            self._last_log_count += 1
            self._replace_last_log_line(f"{shortened} (x{self._last_log_count})")
            return
        self._last_log_text = shortened
        self._last_log_count = 1
        self.log_view.appendPlainText(shortened)

    def _replace_last_log_line(self, text: str) -> None:
        cursor = self.log_view.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        cursor.select(QtGui.QTextCursor.BlockUnderCursor)
        cursor.removeSelectedText()
        cursor.insertText(text)
        cursor.movePosition(QtGui.QTextCursor.End)
        self.log_view.setTextCursor(cursor)

    def _set_interaction_enabled(self, enabled: bool) -> None:
        for btn in self.header_buttons:
            btn.setEnabled(enabled)

        if hasattr(self, "btn_emergency_override"):
            self.btn_emergency_override.setEnabled(enabled)

        if self.left_panel is not None:
            self.left_panel.setEnabled(enabled)

        for col in getattr(self, "columns", []):
            col.setEnabled(enabled)

        self.log_view.setEnabled(True)

    def _log_op_start(self, title: str, detail: str = "") -> str:
        self._op_seq += 1
        op_id = f"op{self._op_seq:04d}"
        now = QtCore.QDateTime.currentDateTime().toString("yyyy/MM/dd HH:mm:ss")
        text = f"{now} {title} … {detail if detail else '実行中'}"
        self.log_view.appendPlainText(text)
        block_no = self.log_view.textCursor().blockNumber()
        self._op_lines[op_id] = block_no
        return op_id

    def _op_mark_ok(self, op_id: str, sign: str) -> None:
        r = self._op_results.setdefault(op_id, {"ok": set(), "ng": {}})
        r["ok"].add(sign)
        r["ng"].pop(sign, None)

    def _op_mark_ng(self, op_id: str, sign: str, reason: str) -> None:
        r = self._op_results.setdefault(op_id, {"ok": set(), "ng": {}})
        r["ng"][sign] = reason
        r["ok"].discard(sign)

    def _set_op_done_label(self, op_id: str, label: str) -> None:
        if label:
            self._op_done_labels[op_id] = label

    def _classify_result_reason(self, reason: str) -> str:
        if not reason:
            return ""
        lower = reason.lower()
        if "timeout" in lower:
            return "timeout"
        if "permission" in lower or "access" in lower or "権限" in reason or "アクセス" in reason:
            return "permission"
        if "json" in lower:
            return "json_error"
        if "到達不可" in reason or "unreachable" in lower or "smb_unreachable" in lower:
            return "unreachable"
        return reason

    def _build_pc_result(self, state: SignState, ok: bool, reason: str, phase: str) -> dict:
        return {
            "pc_id": state.name.replace("Sign", ""),
            "ok": bool(ok),
            "reason": self._classify_result_reason(reason),
            "phase": phase,
        }

    def _apply_pc_results(self, op_id: str, results: List[dict]) -> None:
        if not op_id:
            return
        for result in results:
            sign = result.get("pc_id") or ""
            ok = bool(result.get("ok"))
            reason = result.get("reason") or "error"
            if ok:
                self._ui_call(lambda oid=op_id, s=sign: self._op_mark_ok(oid, s))
            else:
                self._ui_call(lambda oid=op_id, s=sign, r=reason: self._op_mark_ng(oid, s, r))

    def _op_format_result(self, op_id: str) -> str:
        r = self._op_results.get(op_id, {"ok": set(), "ng": {}})
        ok_count = len(r["ok"])
        ng_count = len(r["ng"])
        return f" OK:{ok_count} 失敗:{ng_count}"

    def _log_op_append(self, op_id: str, suffix: str) -> None:
        block_no = self._op_lines.get(op_id)
        if block_no is None:
            return
        doc = self.log_view.document()
        block = doc.findBlockByNumber(block_no)
        if not block.isValid():
            return
        cursor = QtGui.QTextCursor(block)
        cursor.movePosition(QtGui.QTextCursor.EndOfBlock)
        cursor.insertText(suffix)
        self.log_view.moveCursor(QtGui.QTextCursor.End)

    def _log_op_done(self, op_id: str) -> None:
        label = self._op_done_labels.pop(op_id, "")
        if label:
            self._log_op_append(op_id, f" {label}")
        self._log_op_append(op_id, self._op_format_result(op_id))
        self._log_op_append(op_id, " 【完了】")

    def _log_op_error(self, op_id: str, reason: str) -> None:
        self._log_op_append(op_id, self._op_format_result(op_id))
        self._log_op_append(op_id, f" 【エラー】({reason})")

    def _log_reject(self, title: str, reason: str) -> None:
        now = QtCore.QDateTime.currentDateTime().toString("yyyy/MM/dd HH:mm:ss")
        self.log_view.appendPlainText(f"{now} {title} … 【受付不可】({reason})")

    def _dbg(self, msg: str, *args) -> None:
        if not getattr(self, "_dbg_enabled", False):
            return
        try:
            logging.debug("[DBG] " + (msg % args if args else msg))
        except Exception:
            # ログで落ちないように
            try:
                logging.debug("[DBG] %s", msg)
            except Exception:
                pass

    def _dbg_dump_all_threads(self, reason: str) -> None:
        """
        UIがbusyのまま戻らない等、異常系の原因特定用。
        挙動変更はせず、スタックトレースをログに吐くだけ。
        """
        if not getattr(self, "_dbg_enabled", False):
            return
        try:
            buf = io.StringIO()
            buf.write("\n========== THREAD DUMP BEGIN ==========\n")
            buf.write(f"reason={reason}\n")
            buf.write(f"ui_busy={getattr(self, '_ui_busy', None)} busy_label={getattr(self, '_busy_label', '')}\n")
            buf.write(f"last_progress={getattr(self, '_dbg_last_progress', {})}\n")
            buf.write(f"distribute_busy={getattr(self, '_distribute_busy', None)}\n")
            buf.write(f"remote_pending_keys={list(getattr(self, '_remote_status_pending', {}).keys())}\n")
            buf.write("threads:\n")
            for th in threading.enumerate():
                buf.write(f"  - name={th.name} ident={th.ident} daemon={th.daemon} alive={th.is_alive()}\n")
            buf.write("\n-- stacktrace (all threads) --\n")
            faulthandler.dump_traceback(file=buf, all_threads=True)
            buf.write("\n========== THREAD DUMP END ==========\n")
            logging.error("%s", buf.getvalue())
        except Exception as exc:
            try:
                logging.error("[DBG] dump failed: %s", exc)
            except Exception:
                pass

    def _dbg_watchdog_tick(self) -> None:
        if not getattr(self, "_dbg_enabled", False):
            return
        try:
            if not getattr(self, "_ui_busy", False):
                return
            last = getattr(self, "_dbg_last_progress", None) or {}
            ts = float(last.get("ts") or 0.0)
            if ts <= 0:
                return
            elapsed = time_module.monotonic() - ts
            hang_sec = int(getattr(self, "_dbg_hang_seconds", 60))
            if elapsed >= hang_sec:
                # 連打防止：ダンプしたらtsを更新して間隔を空ける
                self._dbg_dump_all_threads(f"UI busy >= {hang_sec}s (elapsed={elapsed:.1f}s)")
                last["ts"] = time_module.monotonic()
                self._dbg_last_progress = last
        except Exception:
            pass

    def _ui_call(self, fn, label: str = "") -> None:
        if not getattr(self, "_dbg_enabled", False):
            QtCore.QTimer.singleShot(0, fn)
            return

        self._dbg_ui_post_seq += 1
        seq = self._dbg_ui_post_seq
        self._dbg_ui_post_pending[seq] = {
            "label": label or getattr(fn, "__name__", "fn"),
            "ts": time_module.monotonic(),
        }
        self._dbg(
            "ui_call enqueue seq=%d label=%s pending=%d",
            seq,
            self._dbg_ui_post_pending[seq]["label"],
            len(self._dbg_ui_post_pending),
        )

        def wrapped():
            try:
                meta = self._dbg_ui_post_pending.pop(seq, None)
                if meta:
                    dt = time_module.monotonic() - float(meta.get("ts") or time_module.monotonic())
                    self._dbg(
                        "ui_call dequeue seq=%d label=%s dt=%.3fs pending=%d",
                        seq,
                        meta.get("label"),
                        dt,
                        len(self._dbg_ui_post_pending),
                    )
            except Exception:
                pass
            fn()

        QtCore.QTimer.singleShot(0, wrapped)

    def run_exclusive_task(
        self,
        title: str,
        worker_fn,
        max_seconds: int = 30,
        detail: str = "実行中",
    ) -> None:
        if self._ui_busy:
            self._dbg("reject title=%s because ui_busy busy_label=%s", title, getattr(self, "_busy_label", ""))
            self._log_reject(title, "作業中")
            return

        self._ui_busy = True
        self._busy_label = title
        self._dbg("ui_busy TRUE title=%s", title)
        self._set_interaction_enabled(False)
        self._dbg("interaction disabled title=%s", title)

        op_id = self._log_op_start(title)
        try:
            self._dbg_last_progress = {
                "op_id": op_id,
                "title": title,
                "token": "start",
                "ts": time_module.monotonic(),
            }
        except Exception:
            pass

        finished = threading.Event()
        cancel_event = threading.Event()
        timeout_timer = QtCore.QTimer(self)
        timeout_timer.setSingleShot(True)

        def progress_token(token: str) -> None:
            if token:
                logging.info("[PROG] %s %s", title, token)
                try:
                    self._dbg_last_progress = {
                        "op_id": op_id,
                        "title": title,
                        "token": token,
                        "ts": time_module.monotonic(),
                    }
                except Exception:
                    pass

        def accepts_op_id() -> bool:
            try:
                sig = inspect.signature(worker_fn)
            except (TypeError, ValueError):
                return False
            params = list(sig.parameters.values())
            if any(p.kind == p.VAR_POSITIONAL for p in params):
                return True
            return len(params) >= 2

        def accepts_cancel_event() -> bool:
            try:
                sig = inspect.signature(worker_fn)
            except (TypeError, ValueError):
                return False
            params = list(sig.parameters.values())
            if any(p.kind == p.VAR_POSITIONAL for p in params):
                return True
            return len(params) >= 3

        def finish_ok() -> None:
            self._dbg("finish_ok called op_id=%s title=%s", op_id, title)
            if finished.is_set():
                return
            if timeout_timer.isActive():
                timeout_timer.stop()
            finished.set()
            self._log_op_done(op_id)
            self._ui_busy = False
            self._busy_label = ""
            self._dbg("ui_busy FALSE op_id=%s title=%s", op_id, title)
            self._set_interaction_enabled(True)
            self._dbg("interaction enabled op_id=%s title=%s", op_id, title)

        def finish_err(reason: str) -> None:
            self._dbg("finish_err called op_id=%s title=%s reason=%s", op_id, title, reason)
            if finished.is_set():
                return
            if timeout_timer.isActive():
                timeout_timer.stop()
            finished.set()
            self._log_op_error(op_id, reason)
            self._ui_busy = False
            self._busy_label = ""
            self._dbg("ui_busy FALSE op_id=%s title=%s", op_id, title)
            self._set_interaction_enabled(True)
            self._dbg("interaction enabled op_id=%s title=%s", op_id, title)

        def handle_timeout() -> None:
            cancel_event.set()
            finish_err("タイムアウト（UI復旧）")

        timeout_timer.timeout.connect(handle_timeout)
        if max_seconds and max_seconds > 0:
            timeout_timer.start(max_seconds * 1000)

        def runner() -> None:
            try:
                self._dbg("runner start op_id=%s title=%s thread=%s", op_id, title, threading.current_thread().name)
                args = [progress_token]
                if accepts_op_id():
                    args.append(op_id)
                if accepts_cancel_event():
                    args.append(cancel_event)
                self._dbg(
                    "worker enter op_id=%s title=%s fn=%s args_len=%d",
                    op_id,
                    title,
                    getattr(worker_fn, "__name__", "worker_fn"),
                    len(args),
                )
                worker_fn(*args)
                self._dbg("worker exit op_id=%s title=%s", op_id, title)
            except Exception as exc:
                self._ui_call(lambda: finish_err(str(exc)), label=f"finish_err:{title}")
            else:
                self._ui_call(finish_ok, label=f"finish_ok:{title}")

        threading.Thread(target=runner, daemon=True).start()

    def _shorten_log_line(self, line: str) -> str:
        trimmed = line.rstrip("\r")
        if not trimmed:
            return trimmed
        if not self.log_view:
            return trimmed
        width = max(10, self.log_view.viewport().width() - 10)
        metrics = QtGui.QFontMetrics(self.log_view.font())
        return metrics.elidedText(trimmed, QtCore.Qt.ElideRight, width)

    def _log_command_accept(self, label: str) -> None:
        logging.info("[CMD] %s 受理", label)

    def _log_command_run(self, label: str) -> None:
        logging.info("[RUN] %s 実行中...", label)

    def _log_command_done(self, label: str, ok_count: int, skip_count: int, err_count: int) -> None:
        logging.info("[DONE] %s 完了 (OK=%d / SKIP=%d / ERR=%d)", label, ok_count, skip_count, err_count)

    def _log_sign_ok(self, state: SignState, message: str) -> None:
        suffix = f" {message}" if message else ""
        logging.info("[OK] %s%s", state.name, suffix)

    def _log_sign_skip(self, state: SignState, reason: str) -> None:
        logging.info("[SKIP] %s %s", state.name, reason)

    def _log_sign_error(self, state: SignState, message: str) -> None:
        logging.info("[ERR] %s %s", state.name, message)

    def _save_inventory_state(self, state: SignState) -> None:
        info = self.inventory.get(state.name, {})
        info["enabled"] = state.enabled
        self.inventory[state.name] = info
        write_json_atomic(INVENTORY_PATH, self.inventory)

    def _load_sign_states(self) -> None:
        for idx in range(1, N_SIGNAGE + 1):
            name = f"Sign{idx:02d}"
            info = self.inventory.get(name, {})
            state = SignState(
                name=name,
                ip=info.get("ip", ""),
                exists=info.get("exists", False),
                share_name=info.get("share_name", "_TsuyamaSignage"),
            )
            state.enabled = info.get("enabled", True)
            self.sign_states[name] = state

    def refresh_summary(self) -> None:
        for col, (name, state) in enumerate(sorted(self.sign_states.items())):
            self._update_column(col, state)
        self.update_ai_badge()

    def _update_column(self, col: int, state: SignState, update_preview: bool = True) -> None:
        column = self._column_widgets.get(state.name.replace("Sign", "Signage"))
        if not column:
            return

        config = read_config(CONFIG_DIR / state.name)
        column.display_label.setText(state.active_channel or "-")
        column.sleep_label.setText(config.get("sleep_channel", "ch01"))
        column.ai_lv2_label.setText(self._display_ai_channel(config.get("ai_channels", {}).get("level2")))
        column.ai_lv3_label.setText(self._display_ai_channel(config.get("ai_channels", {}).get("level3")))
        column.ai_lv4_label.setText(self._display_ai_channel(config.get("ai_channels", {}).get("level4")))
        column.normal_label.setText(config.get("normal_channel", "ch05"))
        column.timer_bar.set_rules(config.get("timer_rules", []))
        column.timer_bar.set_sleep_rules(config.get("sleep_rules", []))

        inactive = (not state.exists) or (not state.enabled)
        column.set_active_state(state.enabled)
        column.set_inactive_style(inactive)
        for btn in [column.setting_button, column.btn_reboot, column.btn_shutdown]:
            btn.setEnabled(state.exists and state.enabled)
        column.btn_active.setEnabled(state.exists)

        if inactive:
            column.set_comm_status(False, None)
        else:
            online = state.online if state.last_update else None
            column.set_comm_status(True, online)

        header_label = self._header_labels.get(state.name.replace("Sign", "Signage"))
        if header_label:
            if inactive:
                header_label.setStyleSheet("border: 1px solid #999; background-color: #c9c9c9; color: #7a7a7a;")
            else:
                header_label.setStyleSheet("border: 1px solid #999;")

        if update_preview:
            self.update_preview_cell(state, column)

    def build_status_text(self, state: SignState) -> str:
        return (
            f"exists={state.exists}\n"
            f"online={state.online}\n"
            f"enabled={state.enabled}\n"
            f"last={state.last_update or '-'}\n"
            f"error={state.last_error or '-'}"
        )

    def build_ai_text(self) -> str:
        level = self.ai_status.get("congestion_level", 1)
        mapping = {
            1: "良好",
            2: "渋滞（LV2）",
            3: "渋滞（LV3）",
            4: "渋滞（LV4）",
        }
        return mapping.get(level, str(level))

    def update_ai_badge(self) -> None:
        if not self.ai_level_badge:
            return
        level = int(self.ai_status.get("congestion_level", 1))
        if level <= 1:
            self.ai_level_badge.setText("渋滞LEVEL1")
            self.ai_level_badge.setStyleSheet(
                "background:#7fd0ff; color:#000; border-radius:8px; font-weight:900; font-size:16px;"
            )
        elif level == 2:
            self.ai_level_badge.setText("渋滞LEVEL2")
            self.ai_level_badge.setStyleSheet(
                "background:#ffb347; color:#000; border-radius:8px; font-weight:900; font-size:16px;"
            )
        elif level == 3:
            self.ai_level_badge.setText("渋滞LEVEL3")
            self.ai_level_badge.setStyleSheet(
                "background:#e53935; color:#fff; border-radius:8px; font-weight:900; font-size:16px;"
            )
        else:
            self.ai_level_badge.setText("渋滞LEVEL4")
            self.ai_level_badge.setStyleSheet(
                "background:#000; color:#fff; border-radius:8px; font-weight:900; font-size:16px;"
            )

    def _display_ai_channel(self, value: Optional[str]) -> str:
        if value == "same_as_normal":
            return AI_CHOICES[0]
        return value or "-"

    def _tcp_probe(self, ip: str, port: int, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _fast_smb_reachable(self, ip: str, timeout_sec: float = 0.2) -> bool:
        """
        UNC(Path.exists/open)を触る前に、SMBポート(445)だけを短時間で確認する。
        ここでNGならUNCアクセスしない（UNCは詰まりやすい）。
        """
        if not ip:
            return False
        return self._tcp_probe(ip, 445, timeout=timeout_sec)

    def is_share_reachable(self, state: SignState) -> Tuple[bool, str]:
        t0 = time_module.monotonic()
        self._dbg("share_reachable start sign=%s ip=%s share=%s", state.name, state.ip, state.share_name)
        ok_ping = is_reachable(state.ip)
        self._dbg("share_reachable ping sign=%s ok=%s dt=%.3fs", state.name, ok_ping, time_module.monotonic() - t0)
        if not ok_ping:
            return False, "到達不可（ping）"
        t1 = time_module.monotonic()
        ok_tcp = self._tcp_probe(state.ip, 445, timeout=1.0)
        self._dbg("share_reachable tcp445 sign=%s ok=%s dt=%.3fs", state.name, ok_tcp, time_module.monotonic() - t1)
        if not ok_tcp:
            return False, "到達不可（tcp445）"
        root = build_unc_path(state.ip, state.share_name, "")
        try:
            t2 = time_module.monotonic()
            exists = Path(root).exists()
            self._dbg("share_reachable unc_exists sign=%s ok=%s dt=%.3fs path=%s", state.name, exists, time_module.monotonic() - t2, root)
            if exists:
                return True, ""
            return False, f"共有に到達できません: {root}"
        except Exception as exc:
            return False, str(exc)

    def refresh_content_request(self) -> None:
        self.run_exclusive_task("動画フォルダ情報更新中", self._task_refresh_content, detail="情報更新 指示送信")

    def _task_refresh_content(self, progress) -> None:
        for state in self.sign_states.values():
            if not state.exists or not state.enabled:
                continue
            progress(state.name)
            column = self._column_widgets.get(state.name.replace("Sign", "Signage"))
            if not column:
                continue
            self._ui_call(lambda s=state, c=column: self.update_preview_cell(s, c))

    def toggle_preview(self) -> None:
        command = "プレビューON/OFF"
        detail = "ON" if not self._preview_enabled else "OFF"
        op_id = self._log_op_start(command, f"{detail} 指示送信")
        self._log_command_accept(command)
        self._log_command_run(command)
        self._preview_enabled = not self._preview_enabled
        ok_count, skip_count, err_count = self.refresh_preview_info(command)
        self._log_command_done(command, ok_count, skip_count, err_count)
        if err_count:
            self._log_op_error(op_id, f"ERR={err_count}")
        else:
            self._log_op_done(op_id)

    def refresh_preview_info(self, log_label: Optional[str] = None) -> Tuple[int, int, int]:
        ok_count = 0
        skip_count = 0
        err_count = 0
        for state in self.sign_states.values():
            column = self._column_widgets.get(state.name.replace("Sign", "Signage"))
            if not column:
                continue
            if not state.exists:
                skip_count += 1
                if log_label:
                    self._log_sign_skip(state, "到達不可")
                self.update_preview_cell(state, column)
                continue
            if not state.enabled:
                skip_count += 1
                if log_label:
                    self._log_sign_skip(state, "非アクティブ")
                self.update_preview_cell(state, column)
                continue
            try:
                self.update_preview_cell(state, column)
                ok_count += 1
                if log_label:
                    self._log_sign_ok(state, "プレビュー更新")
            except Exception as exc:
                err_count += 1
                if log_label:
                    self._log_sign_error(state, str(exc))
        return ok_count, skip_count, err_count

    def update_preview_cell(self, state: SignState, column: SignageColumnWidget) -> None:
        if not state.exists or not state.enabled:
            column.current_channel = None
            column.sample_list = []
            column.show_preview_message("非アクティブ")
            return
        if not self._preview_enabled or not state.active_channel:
            column.current_channel = None
            column.sample_list = []
            column.show_preview_message("プレビューOFF")
            return
        samples = self.list_sample_videos(state.active_channel)
        if not samples:
            column.show_preview_message("サンプルなし")
            return

        if HAS_QTMULTIMEDIA:
            if column.current_channel != state.active_channel or samples != column.sample_list:
                column.current_channel = state.active_channel
                column.set_sample_list(samples)
            return

        sample = samples[0]
        if cv2 is None:
            column.show_preview_message(f"サンプル: {sample.name}")
            return

        frame = self.read_sample_frame(sample)
        if frame is None:
            column.show_preview_message(f"サンプル: {sample.name}")
            return

        height, width, _ = frame.shape
        image = QtGui.QImage(frame.data, width, height, QtGui.QImage.Format_BGR888)
        pixmap = QtGui.QPixmap.fromImage(image).scaled(200, 120, QtCore.Qt.KeepAspectRatio)
        column.show_preview_pixmap(pixmap)

    def list_sample_videos(self, channel: str) -> List[Path]:
        path = CONTENT_DIR / channel
        if not path.exists():
            return []
        samples: List[Path] = []
        for entry in sorted(path.glob("*.mp4")):
            if entry.is_file() and "sample" in entry.name.lower():
                samples.append(entry)
        return samples

    def read_sample_frame(self, file_path: Path):
        capture = cv2.VideoCapture(str(file_path))
        ok, frame = capture.read()
        capture.release()
        if not ok:
            return None
        return frame

    def check_connectivity(self) -> None:
        self.run_exclusive_task(
            "サイネージPC通信確認",
            self._task_check_connectivity,
            max_seconds=45,
            detail="通信確認 指示送信",
        )

    def _task_check_connectivity(self, progress, op_id: Optional[str] = None) -> None:
        timeout = self.settings.get("network_timeout_seconds", 4)
        timeout = float(timeout)
        timeout = max(0.2, min(timeout, 5.0))
        targets = [state for state in self.sign_states.values() if state.exists and state.enabled]
        deadline_seconds = max(1.0, min(12.0, len(targets) * 0.6))
        deadline = time_module.time() + deadline_seconds
        logging.info(
            "通信確認開始: 対象=%s台 timeout=%.2fs deadline=%.2fs",
            len(targets),
            timeout,
            deadline_seconds,
        )

        future_to_state = {
            self._executor.submit(self.check_single_connectivity, state): state
            for state in targets
        }
        pending = set(future_to_state.keys())
        while pending:
            remaining = deadline - time_module.time()
            if remaining <= 0:
                break
            try:
                for future in as_completed(pending, timeout=remaining):
                    pending.discard(future)
                    state = future_to_state[future]
                    progress(state.name)
                    try:
                        online, error, status_note = future.result(timeout=0)
                    except Exception as exc:
                        online, error, status_note = False, str(exc), ""
                    state.online = bool(online)
                    state.last_error = error or ""
                    state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    if status_note and online:
                        logging.info("[WARN] %s 状態未取得 (%s)", state.name, status_note)
                    if op_id:
                        reason = error or ""
                        self._apply_pc_results(op_id, [self._build_pc_result(state, online, reason, "sent")])
                    self._ui_call(
                        lambda s=state: self._update_column(int(s.name.replace("Sign", "")) - 1, s)
                    )
            except FuturesTimeoutError:
                break

        if pending:
            for future in pending:
                future.cancel()
                state = future_to_state[future]
                progress(state.name)
                state.online = False
                state.last_error = "timeout"
                state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if op_id:
                    self._apply_pc_results(
                        op_id,
                        [self._build_pc_result(state, False, state.last_error, "sent")],
                    )
                self._ui_call(
                    lambda s=state: self._update_column(int(s.name.replace("Sign", "")) - 1, s)
                )

    def poll_connectivity_silent(self) -> None:
        timeout = self.settings.get("network_timeout_seconds", 4)
        futures = {}
        for state in self.sign_states.values():
            if not state.exists or not state.enabled:
                if state.online:
                    state.online = False
                    state.last_error = ""
                    state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._ui_call(lambda s=state: self._update_column(int(s.name.replace("Sign", "")) - 1, s))
                continue
            futures[self._executor.submit(self.check_single_connectivity, state)] = state

        for future, state in futures.items():
            try:
                online, error, status_note = future.result(timeout=timeout)
            except FuturesTimeoutError:
                online, error, status_note = False, "timeout", ""
            except Exception as exc:
                online, error, status_note = False, str(exc), ""

            if online != state.online:
                state.online = online
                state.last_error = error or ""
                state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if online:
                    logging.info("[POLL] %s オンライン", state.name)
                else:
                    logging.info("[POLL] %s オフライン (%s)", state.name, error or "offline")
                self._ui_call(lambda s=state: self._update_column(int(s.name.replace("Sign", "")) - 1, s))
            if status_note and online:
                logging.info("[POLL] %s 状態未取得 (%s)", state.name, status_note)

    def check_single_connectivity(self, state: SignState) -> Tuple[bool, str, str]:
        if not self._tcp_probe(state.ip, 445, timeout=1.0):
            return False, "unreachable", ""
        status_note = ""
        try:
            status = self.load_pc_status(state)
            if not status.get("ok"):
                status_note = status.get("error") or "status_unreadable"
        except Exception as exc:
            status_note = str(exc)
        return True, "", status_note

    def recompute_all(self, auto_distribute: bool = True) -> None:
        with self._update_lock:
            self.ai_status = load_json(AI_STATUS_PATH, self.ai_status)
            self.update_ai_badge()
            now = datetime.now()
            updated_any = False
            for state in self.sign_states.values():
                config = read_config(CONFIG_DIR / state.name)
                if not state.exists or not state.enabled:
                    state.active_channel = None
                    continue
                if self._emergency_override_enabled:
                    active_channel = self._emergency_override_channel
                else:
                    active_channel = compute_active_channel(config, self.ai_status, now)
                if state.active_channel != active_channel:
                    updated_any = True
                state.active_channel = active_channel
                write_json_atomic(CONFIG_DIR / state.name / "active.json", {"active_channel": active_channel})
            self.refresh_summary()

        if auto_distribute and updated_any and self.settings.get("auto_distribute_on_event", False):
            self.distribute_all()

    def bulk_update(self) -> None:
        self.run_exclusive_task("一斉Ch更新", self._task_bulk_update, detail="一斉更新 指示送信")

    def _task_bulk_update(self, progress) -> None:
        self.recompute_all(auto_distribute=False)
        for state in self.sign_states.values():
            if not state.exists or not state.enabled:
                continue
            progress(state.name)
        _, _, err_count = self.distribute_all("一斉Ch更新")
        if err_count:
            raise RuntimeError(f"配布エラー {err_count} 台")

    def toggle_emergency_override(self, enabled: bool) -> None:
        if self._ui_busy:
            blocker = QtCore.QSignalBlocker(self.btn_emergency_override)
            self.btn_emergency_override.setChecked(self._emergency_override_enabled)
            del blocker
            self._log_reject("最上位強制メッセージ", "作業中")
            return

        self._emergency_override_enabled = bool(enabled)
        self._apply_emergency_button_style()
        detail = "指示送信中"
        self.run_exclusive_task(
            "最上位強制メッセージ切替中",
            lambda progress, op_id: self._task_apply_emergency_override(progress, op_id, enabled),
            detail=detail,
        )

    def _task_apply_emergency_override(self, progress, op_id: Optional[str], enabled: bool) -> None:
        self.recompute_all(auto_distribute=False)
        results, _, _, _ = self._distribute_all_results(
            "最上位強制メッセージ（20ch）" + (" ON" if enabled else " OFF"),
            phase="sent",
        )
        if op_id:
            self._ui_call(lambda oid=op_id: self._set_op_done_label(oid, "指示送信 完了"))
            self._apply_pc_results(op_id, results)

    def _distribute_all_results(
        self,
        log_label: Optional[str] = None,
        *,
        phase: str = "sent",
    ) -> Tuple[List[dict], int, int, int]:
        if getattr(self, "_distribute_busy", False):
            self._ui_call(lambda: self._log_reject("配布処理", "すでに配布中"))
            return [], 0, 0, 0
        self._distribute_busy = True
        try:
            timeout = self.settings.get("network_timeout_seconds", 4)
            futures = {}
            ok_count = 0
            skip_count = 0
            err_count = 0
            results: List[dict] = []
            for state in self.sign_states.values():
                if not state.exists:
                    skip_count += 1
                    if log_label:
                        self._log_sign_skip(state, "到達不可")
                    continue
                if not state.enabled:
                    skip_count += 1
                    if log_label:
                        self._log_sign_skip(state, "非アクティブ")
                    continue
                futures[self._executor.submit(self.distribute_active, state)] = state

            for future, state in futures.items():
                try:
                    ok, message = future.result(timeout=timeout)
                except FuturesTimeoutError:
                    ok, message = False, "timeout"
                except Exception as exc:
                    ok, message = False, str(exc)
                state.last_error = message if not ok else ""
                state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if ok:
                    ok_count += 1
                    if log_label:
                        self._log_sign_ok(state, "配布完了")
                    results.append(self._build_pc_result(state, True, "", phase))
                else:
                    if "共有" in message or "到達" in message:
                        skip_count += 1
                        if log_label:
                            self._log_sign_skip(state, f"到達不可 ({message})")
                        results.append(self._build_pc_result(state, False, "unreachable", phase))
                    else:
                        err_count += 1
                        if log_label:
                            self._log_sign_error(state, message)
                        results.append(self._build_pc_result(state, False, message, phase))
                self._ui_call(lambda s=state: self._update_column(int(s.name.replace("Sign", "")) - 1, s))
            return results, ok_count, skip_count, err_count
        finally:
            self._distribute_busy = False

    def distribute_all(self, log_label: Optional[str] = None) -> Tuple[int, int, int]:
        _, ok_count, skip_count, err_count = self._distribute_all_results(log_label)
        return ok_count, skip_count, err_count

    def distribute_active(self, state: SignState) -> Tuple[bool, str]:
        active = read_active(CONFIG_DIR / state.name)
        if not active.get("active_channel"):
            return False, "active_channel missing"
        t0 = time_module.monotonic()
        self._dbg("distribute_active start sign=%s", state.name)
        ok, msg = self.is_share_reachable(state)
        self._dbg(
            "distribute_active share_check sign=%s ok=%s dt=%.3fs msg=%s",
            state.name,
            ok,
            time_module.monotonic() - t0,
            msg,
        )
        if not ok:
            logging.warning("到達不可 %s (%s)", state.name, msg)
            return False, msg
        remote_path = build_unc_path(state.ip, state.share_name, f"{REMOTE_CONFIG_DIR}\\active.json")
        try:
            logging.info("[RUN] %s active.json 書込 -> %s", state.name, remote_path)
            t1 = time_module.monotonic()
            self._dbg("distribute_active write start sign=%s path=%s", state.name, remote_path)
            write_json_atomic_remote(Path(remote_path), active)
            self._dbg(
                "distribute_active write done sign=%s dt=%.3fs",
                state.name,
                time_module.monotonic() - t1,
            )
            logging.info("Distributed active.json to %s", state.name)
            return True, ""
        except Exception as exc:
            logging.error(
                "Failed to distribute to %s (%s: %s)",
                state.name,
                exc.__class__.__name__,
                exc,
            )
            return False, f"{exc.__class__.__name__}: {exc}"

    def start_sync(self) -> None:
        self.run_exclusive_task("動画同期", self._task_sync_all, detail="同期 指示送信")

    def _task_sync_all(self, progress, op_id: Optional[str] = None) -> None:
        timeout = self.settings.get("network_timeout_seconds", 4)
        max_workers = self.settings.get("sync_workers", 4)
        futures = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for state in self.sign_states.values():
                if not state.exists or not state.enabled:
                    continue
                futures[executor.submit(self.sync_sign_content, state, progress)] = state

            results: List[dict] = []
            for future, state in futures.items():
                try:
                    ok, message = future.result(timeout=timeout)
                except FuturesTimeoutError:
                    ok, message = False, "timeout"
                except Exception as exc:
                    ok, message = False, str(exc)
                state.last_error = message if not ok else ""
                self._ui_call(lambda s=state: self._update_column(int(s.name.replace("Sign", "")) - 1, s))
                results.append(self._build_pc_result(state, ok, message or "", "sent"))
                if not ok:
                    logging.warning("[ERR] %s 同期失敗 (%s)", state.name, message)

        self._apply_pc_results(op_id or "", results)

    def sync_sign_content(self, state: SignState, progress_channel=None) -> Tuple[bool, str]:
        logging.info("[RUN] %s 同期開始", state.name)
        self._dbg("sync start sign=%s", state.name)
        t0 = time_module.monotonic()
        ok, msg = self.is_share_reachable(state)
        self._dbg("sync share_check sign=%s ok=%s dt=%.3fs msg=%s", state.name, ok, time_module.monotonic() - t0, msg)
        if not ok:
            return False, msg
        total_copied = 0
        total_updated = 0
        total_deleted = 0
        total_skipped = 0
        total_errors = 0

        def log_line(text: str) -> None:
            logging.info("%s", text)

        for channel in CHANNELS:
            local_dir = CONTENT_DIR / channel
            if not local_dir.exists():
                continue
            remote_content = build_unc_path(state.ip, state.share_name, f"{REMOTE_CONTENT_DIR}\\{channel}")
            remote_dir = Path(remote_content)
            t1 = time_module.monotonic()
            exists = remote_dir.exists()
            self._dbg(
                "sync ch=%s remote_exists sign=%s exists=%s dt=%.3fs path=%s",
                channel,
                state.name,
                exists,
                time_module.monotonic() - t1,
                str(remote_dir),
            )
            if not exists:
                return False, f"remote content missing: {remote_content}"
            if progress_channel:
                ch_num = channel.replace("ch", "").lstrip("0")
                progress_channel(f"{ch_num}ch")
            t2 = time_module.monotonic()
            result = sync_mirror_dir(
                local_dir,
                remote_dir,
                logger=log_line,
            )
            self._dbg(
                "sync ch=%s done sign=%s dt=%.3fs res=%s",
                channel,
                state.name,
                time_module.monotonic() - t2,
                result,
            )
            total_copied += result["copied"]
            total_updated += result["updated"]
            total_deleted += result["deleted"]
            total_skipped += result["skipped"]
            total_errors += result["errors"]
        logging.info(
            "[DONE] %s 完了 ADD=%d UPD=%d DEL=%d SKIP=%d ERR=%d",
            state.name,
            total_copied,
            total_updated,
            total_deleted,
            total_skipped,
            total_errors,
        )
        if total_errors:
            return False, f"コピー/削除失敗({total_errors})"
        return True, ""

    def collect_logs(self) -> None:
        self.run_exclusive_task("LOG回収中", self._task_collect_logs, detail="ログ回収 指示送信")

    def _task_collect_logs(self, progress, op_id: Optional[str] = None) -> None:
        timeout = self.settings.get("network_timeout_seconds", 4)
        futures = {}
        results: List[dict] = []
        for state in self.sign_states.values():
            if not state.exists or not state.enabled:
                continue
            futures[self._executor.submit(self.fetch_logs_for_sign, state)] = state
        for future, state in futures.items():
            progress(state.name)
            try:
                ok, message = future.result(timeout=timeout)
            except FuturesTimeoutError:
                ok, message = False, "timeout"
            except Exception as exc:
                ok, message = False, str(exc)
            state.last_error = message if not ok else ""
            results.append(self._build_pc_result(state, ok, message or "", "sent"))
            if not ok:
                logging.warning("[ERR] %s LOG回収失敗 (%s)", state.name, message)
            self._ui_call(lambda s=state: self._update_column(int(s.name.replace("Sign", "")) - 1, s))
        self._apply_pc_results(op_id or "", results)

    def fetch_logs_for_sign(self, state: SignState) -> Tuple[bool, str]:
        self._dbg("fetch_logs start sign=%s", state.name)
        t0 = time_module.monotonic()
        ok, msg = self.is_share_reachable(state)
        self._dbg("fetch_logs share_check sign=%s ok=%s dt=%.3fs msg=%s", state.name, ok, time_module.monotonic() - t0, msg)
        if not ok:
            return False, msg
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_root = Path(self.settings.get("log_backup_dir", str(ROOT_DIR.parent / "backup" / "logs")))
        dest = backup_root / state.name / timestamp
        ensure_dir(dest)
        remote_logs = build_unc_path(state.ip, state.share_name, REMOTE_LOGS_DIR)
        try:
            t1 = time_module.monotonic()
            exists = Path(remote_logs).exists()
            self._dbg(
                "fetch_logs exists sign=%s exists=%s dt=%.3fs path=%s",
                state.name,
                exists,
                time_module.monotonic() - t1,
                remote_logs,
            )
            if not exists:
                return False, "remote logs missing"
            t2 = time_module.monotonic()
            copied = 0
            entries = 0
            for entry in Path(remote_logs).iterdir():
                entries += 1
                if entry.is_file():
                    shutil.copy2(entry, dest / entry.name)
                    copied += 1
            self._dbg(
                "fetch_logs iterdir sign=%s files=%d copied=%d dt=%.3fs",
                state.name,
                entries,
                copied,
                time_module.monotonic() - t2,
            )
            logging.info("Logs fetched for %s", state.name)
            return True, ""
        except Exception as exc:
            logging.exception("Failed log fetch for %s", state.name)
            return False, str(exc)

    def open_config_dialog(self, state: SignState) -> None:
        config_path = CONFIG_DIR / state.name / "config.json"
        config = read_config(CONFIG_DIR / state.name)
        dialog = ConfigDialog(sign_name=state.name, config=config, parent=self, controller_window=self)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            new_config = dialog.get_config()
            if not new_config:
                return
            write_json_atomic(config_path, new_config)
            logging.info("Config saved for %s", state.name)
            self.recompute_all()

    def send_power_command(self, state: SignState, command: str) -> None:
        cmd_label = "再起動" if command == "reboot" else "シャットダウン"
        confirm = QtWidgets.QMessageBox.question(
            self,
            "確認",
            f"{state.name} を {cmd_label} しますか？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        op_id = self._log_op_start("電源操作", f"{state.name} {cmd_label} 指示送信（確認中）")
        ok, msg = self.is_share_reachable(state)
        if not ok:
            state.last_error = msg
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)
            self._log_op_error(op_id, msg)
            return
        command_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{state.name}"
        payload = {
            "command_id": command_id,
            "action": command,
            "force": True,
            "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "command": command,
            "by": "controller",
        }
        remote_path = build_unc_path(state.ip, state.share_name, f"{REMOTE_CONFIG_DIR}\\command.json")
        try:
            write_json_atomic_remote(Path(remote_path), payload)
            logging.info("Power command %s sent to %s", command, state.name)
            sign_no = state.name.replace("Sign", "")
            self._op_mark_ok(op_id, sign_no)
            self._pc_status_skip_until[state.name] = time_module.monotonic() + 60

            def monitor_offline() -> None:
                deadline = time_module.monotonic() + 120
                while time_module.monotonic() < deadline:
                    if not self._tcp_probe(state.ip, 445, timeout=1.0):
                        self._ui_call(lambda oid=op_id: self._log_op_append(oid, " 実行確認OK"))
                        break
                    time_module.sleep(3)
            threading.Thread(target=monitor_offline, daemon=True).start()
            self._log_op_done(op_id)
        except Exception as exc:
            state.last_error = str(exc)
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)
            self._log_op_error(op_id, str(exc))

    def _get_state_by_sign_name(self, sign_name: str) -> Optional[SignState]:
        return self.sign_states.get(sign_name)

    def _on_column_config(self, sign_name: str) -> None:
        state = self._get_state_by_sign_name(sign_name)
        if state:
            self.open_config_dialog(state)

    def _on_column_reboot(self, sign_name: str) -> None:
        state = self._get_state_by_sign_name(sign_name)
        if state:
            self.send_power_command(state, "reboot")

    def _on_column_shutdown(self, sign_name: str) -> None:
        state = self._get_state_by_sign_name(sign_name)
        if state:
            self.send_power_command(state, "shutdown")

    def log(self, message: str) -> None:
        logging.info("%s", message)

    def _on_column_active_toggle(self, sign_id: str, active: bool) -> None:
        try:
            pc_no = int(sign_id.replace("Sign", ""))
        except ValueError:
            logging.info("[ERROR] active toggle: invalid sign_id %s", sign_id)
            return

        state = self.sign_states.get(sign_id)
        if not state:
            logging.info("[ERROR] active toggle: state missing %s", sign_id)
            return
        if state.enabled == active:
            return

        before_label = "アクティブ" if state.enabled else "非アクティブ"
        after_label = "アクティブ" if active else "非アクティブ"
        logging.info("[CMD] %s %s->%s", state.name, before_label, after_label)
        op_id = self._log_op_start("稼働設定", f"{state.name} {after_label} 指示送信")

        state.enabled = active
        self._save_inventory_state(state)
        self._update_column(pc_no - 1, state, update_preview=False)
        self._log_op_done(op_id)

        column = self._column_widgets.get(sign_id.replace("Sign", "Signage"))
        if column:
            if not active:
                column.show_preview_message("非アクティブ")
            else:
                column.show_preview_message("プレビューOFF")

    def check_timer_transition(self) -> None:
        self.recompute_all()

    def schedule_recompute(self) -> None:
        QtCore.QTimer.singleShot(0, self.recompute_all)

    def start_watchers(self) -> None:
        if not WATCHDOG_AVAILABLE:
            logging.warning("watchdog not available, fallback to polling")
            self.poll_ai_timer = QtCore.QTimer(self)
            self.poll_ai_timer.setInterval(60 * 1000)
            self.poll_ai_timer.timeout.connect(self.check_ai_status_polling)
            self.poll_ai_timer.start()
            return

        handler = AiStatusHandler(self.schedule_recompute)
        observer = Observer()
        observer.schedule(handler, str(AI_STATUS_PATH.parent), recursive=False)
        observer.start()
        self._observer = observer

    def check_ai_status_polling(self) -> None:
        try:
            mtime = AI_STATUS_PATH.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if self._ai_status_mtime is None:
            self._ai_status_mtime = mtime
            return
        if mtime != self._ai_status_mtime:
            self._ai_status_mtime = mtime
            self.schedule_recompute()

    def closeEvent(self, event):
        if self._observer:
            self._observer.stop()
            self._observer.join()
        self._executor.shutdown(wait=False)
        if self._log_handler:
            logging.getLogger().removeHandler(self._log_handler)
        super().closeEvent(event)


def setup_logging():
    log_path = LOG_DIR / f"controller_{datetime.now().strftime('%Y%m%d')}.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s")
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers = []
    root.addHandler(handler)


def main():
    try:
        setup_logging()
    except Exception:
        pass

    try:
        app = QtWidgets.QApplication(sys.argv)
        try:
            app.setStyle("windowsvista")
        except Exception:
            try:
                app.setStyle("windows")
            except Exception:
                pass
        window = ControllerWindow()
        window.showMaximized()
        window.recompute_all()
        sys.exit(app.exec())
    except Exception:
        tb = traceback.format_exc()
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            crash = LOG_DIR / f"crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            crash.write_text(tb, encoding="utf-8")
        except Exception:
            pass
        try:
            print(tb, file=sys.__stderr__)
        except Exception:
            pass
        raise SystemExit(1)


if __name__ == "__main__":
    main()
