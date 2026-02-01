import json
import importlib.util
import logging
import os
import shutil
import sys
import threading
import time as time_module
import traceback
from concurrent.futures import ThreadPoolExecutor
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
REMOTE_LOGS_DIR = f"{REMOTE_APP_DIR}\\logs"
REMOTE_CONTENT_DIR = "content"

INVENTORY_PATH = CONFIG_DIR / "inventory.json"
AI_STATUS_PATH = CONFIG_DIR / "ai_status.json"
SETTINGS_PATH = CONFIG_DIR / "controller_settings.json"

CHANNELS = [f"ch{idx:02d}" for idx in range(1, 21)]
NORMAL_CHOICES = [f"ch{n:02d}" for n in range(5, 11)]
TIMER_CHOICES = [f"ch{n:02d}" for n in range(11, 21)]
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
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        logging.exception("Failed to parse %s", path)
        return default


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


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
    tmp.replace(dst)


SYNC_EXTS = {".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp"}


def sync_mirror_dir(
    master_dir: Path,
    remote_dir: Path,
    logger=None,
    dry_run: bool = False,
    compare_ctime: bool = True,
) -> Dict[str, int]:
    result = {"copied": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}
    ensure_dir(remote_dir)

    master_files: Dict[str, Path] = {}
    for entry in master_dir.rglob("*"):
        if entry.is_file() and entry.suffix.lower() in SYNC_EXTS:
            rel = entry.relative_to(master_dir).as_posix()
            master_files[rel] = entry

    remote_files: Dict[str, Path] = {}
    for entry in remote_dir.rglob("*"):
        if entry.is_file() and entry.suffix.lower() in SYNC_EXTS:
            rel = entry.relative_to(remote_dir).as_posix()
            remote_files[rel] = entry

    for rel, rp in sorted(remote_files.items()):
        if rel not in master_files:
            try:
                if logger:
                    logger(f"[DEL] {rel}")
                if not dry_run:
                    rp.unlink()
                result["deleted"] += 1
            except Exception as exc:
                if logger:
                    logger(f"[ERR] delete {rel}: {exc}")
                result["errors"] += 1

    for rel, mp in sorted(master_files.items()):
        rp = remote_dir / Path(rel)
        try:
            if rp.exists():
                if is_same_file(mp, rp, compare_ctime=compare_ctime):
                    result["skipped"] += 1
                else:
                    if logger:
                        logger(f"[UPD] {rel}")
                    if not dry_run:
                        copy_file_atomic(mp, rp)
                    result["updated"] += 1
            else:
                if logger:
                    logger(f"[ADD] {rel}")
                if not dry_run:
                    copy_file_atomic(mp, rp)
                result["copied"] += 1
        except Exception as exc:
            if logger:
                logger(f"[ERR] copy {rel}: {exc}")
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
    os.replace(tmp_path, path)


def write_json_atomic_remote(path: Path, payload: dict) -> None:
    """
    UNC(ネットワーク共有)向け: 親ディレクトリ作成はしない。
    （リモート側のフォルダ構成は前提として存在する）
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    bak_path = path.with_suffix(path.suffix + ".bak")
    try:
        if path.exists():
            shutil.copy2(path, bak_path)
    except Exception:
        pass
    with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)


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
    def __init__(self, sign_name: str, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{sign_name} 設定")
        self.config = config

        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()

        self.sleep_combo = QtWidgets.QComboBox()
        self.sleep_combo.addItems([SLEEP_FIXED])
        self.sleep_combo.setCurrentText(SLEEP_FIXED)
        self.sleep_combo.setEnabled(False)
        form.addRow("休眠チャンネル", self.sleep_combo)

        self.normal_combo = QtWidgets.QComboBox()
        self.normal_combo.addItems(NORMAL_CHOICES)
        normal_value = config.get("normal_channel", "ch05")
        if normal_value not in NORMAL_CHOICES:
            normal_value = NORMAL_CHOICES[0]
        self.normal_combo.setCurrentText(normal_value)
        form.addRow("通常チャンネル", self.normal_combo)

        self.ai_level2 = QtWidgets.QComboBox()
        self.ai_level2.addItems(AI_CHOICES)
        self.ai_level2.setCurrentText(self._ai_choice_to_display(config.get("ai_channels", {}).get("level2")))
        form.addRow("AI LV2", self.ai_level2)

        self.ai_level3 = QtWidgets.QComboBox()
        self.ai_level3.addItems(AI_CHOICES)
        self.ai_level3.setCurrentText(self._ai_choice_to_display(config.get("ai_channels", {}).get("level3")))
        form.addRow("AI LV3", self.ai_level3)

        self.ai_level4 = QtWidgets.QComboBox()
        self.ai_level4.addItems(AI_CHOICES)
        self.ai_level4.setCurrentText(self._ai_choice_to_display(config.get("ai_channels", {}).get("level4")))
        form.addRow("AI LV4", self.ai_level4)

        layout.addLayout(form)

        layout.addWidget(QtWidgets.QLabel("休眠時間帯"))
        self.sleep_table = QtWidgets.QTableWidget(1, 2)
        self.sleep_table.setHorizontalHeaderLabels(["開始", "終了"])
        self.sleep_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.sleep_table)
        self._set_sleep_rule(config.get("sleep_rules", []))

        self.timer_table = QtWidgets.QTableWidget(0, 3)
        self.timer_table.setHorizontalHeaderLabels(["開始", "終了", "CH"])
        self.timer_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        delegate = TimeNormalizeDelegate(self.timer_table, self.timer_table)
        self.timer_table.setItemDelegateForColumn(0, delegate)
        self.timer_table.setItemDelegateForColumn(1, delegate)
        layout.addWidget(QtWidgets.QLabel("タイマー設定"))
        layout.addWidget(self.timer_table)

        for rule in config.get("timer_rules", []):
            self.add_timer_rule(rule)

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
        action_layout.addStretch()
        action_layout.addWidget(save_button)
        action_layout.addWidget(cancel_button)
        layout.addLayout(action_layout)

        self._built_config = None
        save_button.clicked.connect(self._on_save)
        cancel_button.clicked.connect(self.reject)

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
        self._enabled = True

    def set_rules(self, rules: List[dict]) -> None:
        self._rules = rules
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

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
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

        self.setting_button.setStyleSheet("background:#ffffff;")
        self.btn_reboot.setStyleSheet("background:#e8f4ff;")
        self.btn_shutdown.setStyleSheet("background:#ffecec;")

        self.setting_button.clicked.connect(
            lambda: self.clicked_config.emit(self.name.replace("Signage", "Sign"))
        )
        self.btn_reboot.clicked.connect(
            lambda: self.clicked_reboot.emit(self.name.replace("Signage", "Sign"))
        )
        self.btn_shutdown.clicked.connect(
            lambda: self.clicked_shutdown.emit(self.name.replace("Signage", "Sign"))
        )
        self.btn_active.toggled.connect(
            lambda checked: self.toggled_active.emit(self.name.replace("Signage", "Sign"), checked)
        )
        self.set_comm_status(True, None)

    def _make_label(self, text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet("border: 1px solid #999;")
        return label

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
            self.comm_label.setText("非アクティブ")
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
        self.sys_panel: Optional[QtWidgets.QWidget] = None
        self.remote_status_panel: Optional[QtWidgets.QWidget] = None
        self.header_buttons: List[QtWidgets.QPushButton] = []
        self.columns: List[SignageColumnWidget] = []
        self.chip_cpu_load: Optional[QtWidgets.QLabel] = None
        self.chip_cpu_temp: Optional[QtWidgets.QLabel] = None
        self.chip_gpu_temp: Optional[QtWidgets.QLabel] = None
        self.chip_ssd_temp: Optional[QtWidgets.QLabel] = None
        self.chip_chipset_temp: Optional[QtWidgets.QLabel] = None
        self.lb_ssd_usage: Optional[QtWidgets.QLabel] = None
        self.remote_status_labels: Dict[str, Dict[str, QtWidgets.QLabel]] = {}
        self._remote_status_spacers: Dict[str, QtWidgets.QLabel] = {}
        self._remote_status_cache: Dict[str, dict] = {}
        self._remote_status_pending: Dict[str, dict] = {}
        self._remote_status_log_state: Dict[str, str] = {}
        self._telemetry_timer: Optional[QtCore.QTimer] = None

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
        self._telemetry_timer.setInterval(2000)
        self._telemetry_timer.timeout.connect(self.refresh_local_telemetry)
        self._telemetry_timer.timeout.connect(self.refresh_remote_telemetry)
        self._telemetry_timer.start()
        self.refresh_local_telemetry()
        self.refresh_remote_telemetry()

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
        self.btn_check = QtWidgets.QPushButton("サイネージPC通信確認")
        self.btn_bulk_update = QtWidgets.QPushButton("一斉Ch更新")
        self.btn_refresh_content = QtWidgets.QPushButton("フォルダ内動画情報取得")
        self.btn_sync = QtWidgets.QPushButton("動画の同期開始")
        self.btn_logs = QtWidgets.QPushButton("LOGファイル取得")
        self.btn_preview_toggle = QtWidgets.QPushButton("プレビューON/OFF")

        for btn in [
            self.btn_check,
            self.btn_bulk_update,
            self.btn_refresh_content,
            self.btn_sync,
            self.btn_logs,
            self.btn_preview_toggle,
        ]:
            button_layout.addWidget(btn)

        header_layout.addStretch()
        header_layout.addLayout(button_layout)
        self.ai_level_badge = QtWidgets.QLabel("LEVEL1")
        self.ai_level_badge.setAlignment(QtCore.Qt.AlignCenter)
        self.ai_level_badge.setFixedSize(160, 44)
        self.ai_level_badge.setStyleSheet("border-radius:8px; font-weight:900; font-size:16px;")
        header_layout.addWidget(self.ai_level_badge)
        layout.addLayout(header_layout)

        header_row = QtWidgets.QHBoxLayout()
        left_header = QtWidgets.QLabel("")
        left_header.setFixedSize(LEFT_COL_WIDTH, 42)
        header_row.addWidget(left_header)

        header_container = QtWidgets.QWidget()
        header_container_layout = QtWidgets.QHBoxLayout(header_container)
        header_container_layout.setContentsMargins(0, 0, 0, 0)
        header_container_layout.setSpacing(GAP_PX)

        self.header_buttons = []
        for idx in range(1, 21):
            name = f"Signage {idx:02d}"
            button = QtWidgets.QPushButton(name)
            button.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
            button.setFixedHeight(42)
            button.setStyleSheet("border: 1px solid #999;")
            header_container_layout.addWidget(button)
            self.header_buttons.append(button)
            self._header_labels[name.replace("Signage ", "Signage")] = button

        header_container_layout.addStretch()
        header_row.addWidget(header_container)
        layout.addLayout(header_row)

        body_layout = QtWidgets.QHBoxLayout()
        self.left_panel = QtWidgets.QWidget()
        self.left_panel.setFixedWidth(LEFT_COL_WIDTH)
        left_layout = QtWidgets.QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(2, 2, 2, 2)
        left_layout.setSpacing(2)

        left_layout.addWidget(self._make_row_label("表示中ch", 28))
        left_layout.addWidget(self._make_row_label("表示中映像", 105))
        left_layout.addWidget(self._make_row_label("設定", 26))
        left_layout.addWidget(self._make_row_label("休眠時", 20))
        left_layout.addWidget(self._make_row_label("AI渋滞判定(LV2)時", 20))
        left_layout.addWidget(self._make_row_label("AI渋滞判定(LV3)時", 20))
        left_layout.addWidget(self._make_row_label("AI渋滞判定(LV4)時", 20))
        left_layout.addWidget(self._make_row_label("通常時", 20))
        left_layout.addWidget(self._make_row_label("タイマー設定", 20))
        timer_label = TimerLegendWidget()
        timer_label.setFixedHeight(220)
        left_layout.addWidget(timer_label)
        left_layout.addWidget(self._make_row_label("サイネージPC\n電源管理", 52))
        left_layout.addWidget(self._make_row_label("管理する\nサイネージ", 52))

        body_layout.addWidget(self.left_panel)

        body_container = QtWidgets.QWidget()
        body_container_layout = QtWidgets.QHBoxLayout(body_container)
        body_container_layout.setContentsMargins(0, 0, 0, 0)
        body_container_layout.setSpacing(GAP_PX)

        self.columns = []
        for idx in range(1, 21):
            name = f"Signage{idx:02d}"
            column = SignageColumnWidget(name)
            column.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
            column.setMinimumWidth(0)
            self.columns.append(column)
            body_container_layout.addWidget(column)
            self._column_widgets[name] = column
            column.clicked_config.connect(self._on_column_config)
            column.clicked_reboot.connect(self._on_column_reboot)
            column.clicked_shutdown.connect(self._on_column_shutdown)
            column.toggled_active.connect(self._on_column_active_toggle)

        body_container_layout.addStretch()
        body_layout.addWidget(body_container)
        layout.addLayout(body_layout)

        status_label = QtWidgets.QLabel("Controller PC状態（CPU/温度）")
        status_label.setFixedHeight(16)
        status_label.setStyleSheet("color:#666;")
        layout.addWidget(status_label)

        self.sys_panel = QtWidgets.QWidget()
        sys_layout = QtWidgets.QVBoxLayout(self.sys_panel)
        sys_layout.setContentsMargins(0, 0, 0, 0)
        sys_layout.setSpacing(2)
        self.sys_panel.setFixedHeight(74)

        chips_row = QtWidgets.QWidget()
        chips_layout = QtWidgets.QHBoxLayout(chips_row)
        chips_layout.setContentsMargins(0, 0, 0, 0)
        chips_layout.setSpacing(6)
        self.chip_cpu_load = self._make_chip("CPU負荷")
        self.chip_cpu_temp = self._make_chip("CPU温度")
        self.chip_gpu_temp = self._make_chip("GPU温度")
        self.chip_ssd_temp = self._make_chip("SSD温度")
        self.chip_chipset_temp = self._make_chip("チップセット")
        for chip in [
            self.chip_cpu_load,
            self.chip_cpu_temp,
            self.chip_gpu_temp,
            self.chip_ssd_temp,
            self.chip_chipset_temp,
        ]:
            chips_layout.addWidget(chip)
        chips_layout.addStretch()

        ssd_row = QtWidgets.QWidget()
        ssd_layout = QtWidgets.QHBoxLayout(ssd_row)
        ssd_layout.setContentsMargins(0, 0, 0, 0)
        ssd_layout.setSpacing(6)
        self.lb_ssd_usage = QtWidgets.QLabel("SSD使用状況 -/-")
        self.lb_ssd_usage.setAlignment(QtCore.Qt.AlignCenter)
        self.lb_ssd_usage.setFixedHeight(20)
        self.lb_ssd_usage.setStyleSheet(
            "background:#ffffff; color:#111; border:1px solid #bbb; border-radius:8px; padding:2px 8px;"
        )
        ssd_layout.addWidget(self.lb_ssd_usage)
        ssd_layout.addStretch()

        sys_layout.addWidget(chips_row)
        sys_layout.addWidget(ssd_row)
        layout.addWidget(self.sys_panel)

        remote_label = QtWidgets.QLabel("サイネージPC状態（CPU/温度）")
        remote_label.setFixedHeight(16)
        remote_label.setStyleSheet("color:#666;")
        layout.addWidget(remote_label)

        self.remote_status_panel = QtWidgets.QWidget()
        remote_panel_layout = QtWidgets.QVBoxLayout(self.remote_status_panel)
        remote_panel_layout.setContentsMargins(0, 0, 0, 0)
        remote_panel_layout.setSpacing(2)
        self.remote_status_panel.setFixedHeight(74)

        remote_cpu_row = QtWidgets.QWidget()
        remote_cpu_layout = QtWidgets.QHBoxLayout(remote_cpu_row)
        remote_cpu_layout.setContentsMargins(0, 0, 0, 0)
        remote_cpu_layout.setSpacing(GAP_PX)
        remote_cpu_spacer = QtWidgets.QLabel("")
        remote_cpu_spacer.setFixedWidth(LEFT_COL_WIDTH)
        remote_cpu_spacer.setFixedHeight(26)
        remote_cpu_layout.addWidget(remote_cpu_spacer)
        self._remote_status_spacers["cpu"] = remote_cpu_spacer

        remote_ssd_row = QtWidgets.QWidget()
        remote_ssd_layout = QtWidgets.QHBoxLayout(remote_ssd_row)
        remote_ssd_layout.setContentsMargins(0, 0, 0, 0)
        remote_ssd_layout.setSpacing(GAP_PX)
        remote_ssd_spacer = QtWidgets.QLabel("")
        remote_ssd_spacer.setFixedWidth(LEFT_COL_WIDTH)
        remote_ssd_spacer.setFixedHeight(20)
        remote_ssd_layout.addWidget(remote_ssd_spacer)
        self._remote_status_spacers["ssd"] = remote_ssd_spacer

        for idx in range(1, 21):
            name = f"Sign{idx:02d}"
            cpu_label = self._make_status_cell()
            cpu_label.setFixedHeight(26)
            ssd_label = self._make_status_cell()
            ssd_label.setFixedHeight(20)
            remote_cpu_layout.addWidget(cpu_label)
            remote_ssd_layout.addWidget(ssd_label)
            self.remote_status_labels[name] = {"cpu": cpu_label, "ssd": ssd_label}

        remote_cpu_layout.addStretch()
        remote_ssd_layout.addStretch()
        remote_panel_layout.addWidget(remote_cpu_row)
        remote_panel_layout.addWidget(remote_ssd_row)
        layout.addWidget(self.remote_status_panel)

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
        self.log_view.setFixedHeight(90)
        layout.addWidget(self.log_view)

        self.setCentralWidget(central)

        self.btn_check.clicked.connect(self.check_connectivity)
        self.btn_bulk_update.clicked.connect(self.bulk_update)
        self.btn_refresh_content.clicked.connect(self.refresh_content_request)
        self.btn_sync.clicked.connect(self.start_sync)
        self.btn_logs.clicked.connect(self.collect_logs)
        self.btn_preview_toggle.clicked.connect(self.toggle_preview)

        self.setStyleSheet(
            """
QPushButton {
  border: 1px solid #666;
  border-radius: 8px;
  padding: 6px 10px;
  background: #f6f6f6;
  font-weight: 700;
}
QPushButton:hover { background: #ffffff; }
QPushButton:pressed { background: #e6e6e6; }
QPushButton:disabled {
  background: #cfcfcf;
  color: #777;
  border: 1px solid #999;
}
"""
        )

        QtCore.QTimer.singleShot(800, self.check_connectivity)

    def apply_dynamic_column_widths(self) -> None:
        total_w = self.centralWidget().width()
        usable = total_w - LEFT_COL_WIDTH - OUTER_MARGIN * 2 - GAP_PX * (20 - 1)
        col_w = max(45, int(usable / 20))

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

        if self._remote_status_spacers:
            for spacer in self._remote_status_spacers.values():
                spacer.setFixedWidth(LEFT_COL_WIDTH)

        if self.remote_status_labels:
            for labels in self.remote_status_labels.values():
                labels["cpu"].setFixedWidth(col_w)
                labels["ssd"].setFixedWidth(col_w)

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
            return "-"
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "-"
        return f"{numeric:.{decimals}f}{unit}"

    def _set_ssd_usage_label(self, label: QtWidgets.QLabel, used_gb: Optional[float], total_gb: Optional[float]) -> None:
        if used_gb is None or total_gb in (None, 0):
            label.setText("SSD使用状況 -")
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

    def _setup_log_stream(self) -> None:
        orig_err = sys.__stderr__

        self._log_stream = EmittingStream(fallback=orig_err)
        self._log_stream.text_written.connect(self.append_log_text)
        sys.stdout = self._log_stream
        sys.stderr = self._log_stream
        handler = LogHandler(self._log_stream)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        self._log_handler = handler

        def excepthook(exc_type, exc_value, exc_traceback):
            formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
            self._log_stream.write(formatted)

        sys.excepthook = excepthook

    def refresh_local_telemetry(self) -> None:
        data: dict = {}
        try:
            if TELEMETRY_LOCAL_PATH.exists():
                with TELEMETRY_LOCAL_PATH.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
        except Exception as exc:
            logging.warning("telemetry_local read failed: %s", exc)
            data = {}

        cpu_load = data.get("cpu_total")
        cpu_temp = data.get("cpu_temp")
        if cpu_temp is None:
            cpu_temp = data.get("cpu_package")
        gpu_temp = data.get("gpu_temp")
        ssd_temp = data.get("ssd_temp")
        chipset_temp = data.get("chipset_temp")
        ssd_used_gb = None
        ssd_total_gb = None
        try:
            usage = shutil.disk_usage(r"C:\\")
            ssd_used_gb = round(usage.used / (1024**3), 1)
            ssd_total_gb = round(usage.total / (1024**3), 1)
        except Exception:
            ssd_used_gb = None
            ssd_total_gb = None

        if self.chip_cpu_load:
            self._chip_set_value(self.chip_cpu_load, cpu_load, "%", "load")
        if self.chip_cpu_temp:
            self._chip_set_value(self.chip_cpu_temp, cpu_temp, "℃", "temp")
        if self.chip_gpu_temp:
            self._chip_set_value(self.chip_gpu_temp, gpu_temp, "℃", "temp")
        if self.chip_ssd_temp:
            self._chip_set_value(self.chip_ssd_temp, ssd_temp, "℃", "temp")
        if self.chip_chipset_temp:
            self._chip_set_value(self.chip_chipset_temp, chipset_temp, "℃", "temp")
        if self.lb_ssd_usage:
            self._set_ssd_usage_label(self.lb_ssd_usage, ssd_used_gb, ssd_total_gb)

    def _remote_status_path(self, state: SignState) -> Path:
        remote_path = build_unc_path(
            state.ip,
            state.share_name,
            f"{REMOTE_LOGS_DIR}\\status\\pc_status.json",
        )
        return Path(remote_path)

    def _read_remote_status(self, state: SignState) -> dict:
        path = self._remote_status_path(state)
        if not path.exists():
            return {"ok": False, "error": "not_found"}
        fingerprint = stat_fingerprint(path)
        cached = self._remote_status_cache.get(state.name)
        if cached and cached.get("fingerprint") == fingerprint:
            return {"ok": True, "payload": cached.get("payload"), "cached": True}
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self._remote_status_cache[state.name] = {"fingerprint": fingerprint, "payload": payload}
        return {"ok": True, "payload": payload, "cached": False}

    def refresh_remote_telemetry(self) -> None:
        timeout_sec = 2.0
        now = time_module.monotonic()
        for state in self.sign_states.values():
            labels = self.remote_status_labels.get(state.name)
            if not labels:
                continue
            if not state.exists:
                self._remote_status_pending.pop(state.name, None)
                self._set_status_error(labels["cpu"], "通信NG")
                self._set_status_error(labels["ssd"], "通信NG")
                continue
            if not state.enabled:
                self._remote_status_pending.pop(state.name, None)
                self._set_status_inactive(labels["cpu"], "非アクティブ")
                self._set_status_inactive(labels["ssd"], "非アクティブ")
                continue

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
                    future.cancel()
                    self._remote_status_pending.pop(state.name, None)
                    self._apply_remote_status(state, {"ok": False, "error": "timeout"})
                continue

            future = self._executor.submit(self._read_remote_status, state)
            self._remote_status_pending[state.name] = {"future": future, "started": now}

    def _apply_remote_status(self, state: SignState, result: dict) -> None:
        labels = self.remote_status_labels.get(state.name)
        if not labels:
            return

        if state.last_update and state.online is False:
            self._set_status_error(labels["cpu"], "通信NG")
            self._set_status_error(labels["ssd"], "通信NG")
            return

        if not result.get("ok"):
            error = result.get("error") or "error"
            message = "通信NG" if error in ("not_found", "timeout") else "エラー"
            self._set_status_error(labels["cpu"], message)
            self._set_status_error(labels["ssd"], message)
            log_line = f"[ERR] {state.name} pc_status取得失敗 ({error})"
            if self._remote_status_log_state.get(state.name) != log_line:
                logging.info("%s", log_line)
                self._remote_status_log_state[state.name] = log_line
            return

        payload = result.get("payload")
        if not isinstance(payload, dict):
            self._set_status_error(labels["cpu"], "エラー")
            self._set_status_error(labels["ssd"], "エラー")
            log_line = f"[ERR] {state.name} pc_status取得失敗 (payload)"
            if self._remote_status_log_state.get(state.name) != log_line:
                logging.info("%s", log_line)
                self._remote_status_log_state[state.name] = log_line
            return

        cpu_load = payload.get("cpu_total_percent")
        cpu_temp = payload.get("cpu_temp_c") or payload.get("cpu_temp") or payload.get("cpu_package")
        gpu_temp = payload.get("gpu_temp_c") or payload.get("gpu_temp")
        chipset_temp = payload.get("chipset_temp_c") or payload.get("chipset_temp")
        ssd_temp = payload.get("ssd", {}).get("temp_c")
        used_gb = payload.get("ssd", {}).get("used_gb")
        total_gb = payload.get("ssd", {}).get("total_gb")
        missing_keys = []
        if cpu_load is None:
            missing_keys.append("cpu_total_percent")
        if cpu_temp is None:
            missing_keys.append("cpu_temp_c")
        if gpu_temp is None:
            missing_keys.append("gpu_temp_c")
        if chipset_temp is None:
            missing_keys.append("chipset_temp_c")
        if ssd_temp is None:
            missing_keys.append("ssd_temp_c")

        severities = [
            self._calc_severity(cpu_load, "load"),
            self._calc_severity(cpu_temp, "temp"),
            self._calc_severity(gpu_temp, "temp"),
            self._calc_severity(ssd_temp, "temp"),
            self._calc_severity(chipset_temp, "temp"),
        ]
        severity_values = [s for s in severities if s is not None]
        if not severity_values:
            self._set_status_error(labels["cpu"], "エラー")
        else:
            cpu_text = (
                f"CPU {self._format_metric(cpu_load, '%')} {self._format_metric(cpu_temp, '℃')}\n"
                f"GPU {self._format_metric(gpu_temp, '℃')} "
                f"SSD {self._format_metric(ssd_temp, '℃')} "
                f"CHP {self._format_metric(chipset_temp, '℃')}"
            )
            self._set_status_label(labels["cpu"], cpu_text, max(severity_values))

        self._set_ssd_usage_label(labels["ssd"], used_gb, total_gb)
        if missing_keys:
            log_line = f"[OK] {state.name} pc_status payload missing: {','.join(missing_keys)}"
        else:
            log_line = f"[OK] {state.name} pc_status payload complete"
        if self._remote_status_log_state.get(state.name) != log_line:
            logging.info("%s", log_line)
            self._remote_status_log_state[state.name] = log_line

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
        for idx in range(1, 21):
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

    def _update_column(self, col: int, state: SignState) -> None:
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
            self.ai_level_badge.setText("LEVEL1")
            self.ai_level_badge.setStyleSheet(
                "background:#7fd0ff; color:#000; border-radius:8px; font-weight:900; font-size:16px;"
            )
        elif level == 2:
            self.ai_level_badge.setText("LEVEL2")
            self.ai_level_badge.setStyleSheet(
                "background:#ffb347; color:#000; border-radius:8px; font-weight:900; font-size:16px;"
            )
        elif level == 3:
            self.ai_level_badge.setText("LEVEL3")
            self.ai_level_badge.setStyleSheet(
                "background:#e53935; color:#fff; border-radius:8px; font-weight:900; font-size:16px;"
            )
        else:
            self.ai_level_badge.setText("LEVEL4")
            self.ai_level_badge.setStyleSheet(
                "background:#000; color:#fff; border-radius:8px; font-weight:900; font-size:16px;"
            )

    def _display_ai_channel(self, value: Optional[str]) -> str:
        if value == "same_as_normal":
            return AI_CHOICES[0]
        return value or "-"


    def is_share_reachable(self, state: SignState) -> Tuple[bool, str]:
        root = build_unc_path(state.ip, state.share_name, "")
        try:
            if Path(root).exists():
                return True, ""
            return False, f"共有に到達できません: {root}"
        except Exception as exc:
            return False, str(exc)

    def refresh_content_request(self) -> None:
        command = "フォルダ内動画情報取得"
        self._log_command_accept(command)
        self._log_command_run(command)
        ok_count, skip_count, err_count = self.refresh_preview_info(command)
        self._log_command_done(command, ok_count, skip_count, err_count)

    def toggle_preview(self) -> None:
        command = "プレビューON/OFF"
        self._log_command_accept(command)
        self._log_command_run(command)
        self._preview_enabled = not self._preview_enabled
        ok_count, skip_count, err_count = self.refresh_preview_info(command)
        self._log_command_done(command, ok_count, skip_count, err_count)

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
        command = "サイネージPC通信確認"
        self._log_command_accept(command)
        self._log_command_run(command)
        timeout = self.settings.get("network_timeout_seconds", 4)
        futures = {}
        ok_count = 0
        skip_count = 0
        err_count = 0
        for state in self.sign_states.values():
            if not state.exists:
                self._log_sign_skip(state, "到達不可")
                skip_count += 1
                continue
            if not state.enabled:
                self._log_sign_skip(state, "非アクティブ")
                skip_count += 1
                continue
            futures[self._executor.submit(self.check_single_connectivity, state)] = state

        for future, state in futures.items():
            try:
                online, error = future.result(timeout=timeout)
                state.online = online
                state.last_error = error or ""
                state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if online:
                    ok_count += 1
                    self._log_sign_ok(state, "オンライン")
                else:
                    err_count += 1
                    self._log_sign_error(state, error or "オフライン")
            except Exception as exc:
                state.online = False
                state.last_error = str(exc)
                err_count += 1
                self._log_sign_error(state, str(exc))
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)
        self._log_command_done(command, ok_count, skip_count, err_count)

    def check_single_connectivity(self, state: SignState) -> Tuple[bool, str]:
        remote_path = build_unc_path(state.ip, state.share_name, REMOTE_CONFIG_DIR)
        try:
            return Path(remote_path).exists(), ""
        except Exception as exc:
            return False, str(exc)

    def recompute_all(self) -> None:
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
                active_channel = compute_active_channel(config, self.ai_status, now)
                if state.active_channel != active_channel:
                    updated_any = True
                state.active_channel = active_channel
                write_json_atomic(CONFIG_DIR / state.name / "active.json", {"active_channel": active_channel})
            self.refresh_summary()

        if updated_any and self.settings.get("auto_distribute_on_event", False):
            self.distribute_all()

    def bulk_update(self) -> None:
        command = "一斉Ch更新"
        self._log_command_accept(command)
        self._log_command_run(command)
        self.recompute_all()
        ok_count, skip_count, err_count = self.distribute_all(command)
        self._log_command_done(command, ok_count, skip_count, err_count)

    def distribute_all(self, log_label: Optional[str] = None) -> Tuple[int, int, int]:
        timeout = self.settings.get("network_timeout_seconds", 4)
        futures = {}
        ok_count = 0
        skip_count = 0
        err_count = 0
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
                state.last_error = message if not ok else ""
                state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if ok:
                    ok_count += 1
                    if log_label:
                        self._log_sign_ok(state, "配布完了")
                else:
                    if "共有" in message or "到達" in message:
                        skip_count += 1
                        if log_label:
                            self._log_sign_skip(state, f"到達不可 ({message})")
                    else:
                        err_count += 1
                        if log_label:
                            self._log_sign_error(state, message)
            except Exception as exc:
                state.last_error = str(exc)
                err_count += 1
                if log_label:
                    self._log_sign_error(state, str(exc))
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)
        return ok_count, skip_count, err_count

    def distribute_active(self, state: SignState) -> Tuple[bool, str]:
        active = read_active(CONFIG_DIR / state.name)
        if not active.get("active_channel"):
            return False, "active_channel missing"
        ok, msg = self.is_share_reachable(state)
        if not ok:
            logging.warning("到達不可 %s (%s)", state.name, msg)
            return False, msg
        remote_path = build_unc_path(state.ip, state.share_name, f"{REMOTE_CONFIG_DIR}\\active.json")
        try:
            logging.info("[RUN] %s active.json 書込 -> %s", state.name, remote_path)
            write_json_atomic_remote(Path(remote_path), active)
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
        command = "動画の同期開始"
        self._log_command_accept(command)
        self._log_command_run(command)
        compare_ctime = self.settings.get("compare_ctime", True)
        logging.info("[RUN] 同期方式: mirror (ADD/UPD/DEL), compare_ctime=%s", compare_ctime)
        timeout = self.settings.get("network_timeout_seconds", 4)
        max_workers = self.settings.get("sync_workers", 4)
        futures = {}
        ok_count = 0
        skip_count = 0
        err_count = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for state in self.sign_states.values():
                if not state.exists:
                    skip_count += 1
                    self._log_sign_skip(state, "到達不可")
                    continue
                if not state.enabled:
                    skip_count += 1
                    self._log_sign_skip(state, "非アクティブ")
                    continue
                futures[executor.submit(self.sync_sign_content, state)] = state
            for future, state in futures.items():
                try:
                    ok, message = future.result(timeout=timeout)
                    state.last_error = message if not ok else ""
                    if ok:
                        ok_count += 1
                        self._log_sign_ok(state, "同期完了")
                    else:
                        if "共有" in message or "到達" in message:
                            skip_count += 1
                            self._log_sign_skip(state, f"到達不可 ({message})")
                        else:
                            err_count += 1
                            self._log_sign_error(state, message)
                except Exception as exc:
                    state.last_error = str(exc)
                    err_count += 1
                    self._log_sign_error(state, str(exc))
                self._update_column(int(state.name.replace("Sign", "")) - 1, state)
        self._log_command_done(command, ok_count, skip_count, err_count)

    def sync_sign_content(self, state: SignState) -> Tuple[bool, str]:
        logging.info("[RUN] %s 同期開始", state.name)
        ok, msg = self.is_share_reachable(state)
        if not ok:
            return False, msg
        compare_ctime = self.settings.get("compare_ctime", True)
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
            if not remote_dir.exists():
                return False, f"remote content missing: {remote_content}"
            result = sync_mirror_dir(
                local_dir,
                remote_dir,
                logger=log_line,
                compare_ctime=compare_ctime,
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
            return False, "sync errors"
        return True, ""

    def collect_logs(self) -> None:
        command = "LOGファイル取得"
        self._log_command_accept(command)
        self._log_command_run(command)
        timeout = self.settings.get("network_timeout_seconds", 4)
        futures = {}
        ok_count = 0
        skip_count = 0
        err_count = 0
        for state in self.sign_states.values():
            if not state.exists:
                skip_count += 1
                self._log_sign_skip(state, "到達不可")
                continue
            if not state.enabled:
                skip_count += 1
                self._log_sign_skip(state, "非アクティブ")
                continue
            futures[self._executor.submit(self.fetch_logs_for_sign, state)] = state
        for future, state in futures.items():
            try:
                ok, message = future.result(timeout=timeout)
                state.last_error = message if not ok else ""
                if ok:
                    ok_count += 1
                    self._log_sign_ok(state, "ログ取得完了")
                else:
                    if "共有" in message or "到達" in message:
                        skip_count += 1
                        self._log_sign_skip(state, f"到達不可 ({message})")
                    else:
                        err_count += 1
                        self._log_sign_error(state, message)
            except Exception as exc:
                state.last_error = str(exc)
                err_count += 1
                self._log_sign_error(state, str(exc))
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)
        self._log_command_done(command, ok_count, skip_count, err_count)

    def fetch_logs_for_sign(self, state: SignState) -> Tuple[bool, str]:
        ok, msg = self.is_share_reachable(state)
        if not ok:
            return False, msg
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_root = Path(self.settings.get("log_backup_dir", str(ROOT_DIR.parent / "backup" / "logs")))
        dest = backup_root / state.name / timestamp
        ensure_dir(dest)
        remote_logs = build_unc_path(state.ip, state.share_name, REMOTE_LOGS_DIR)
        try:
            if not Path(remote_logs).exists():
                return False, "remote logs missing"
            for entry in Path(remote_logs).iterdir():
                if entry.is_file():
                    shutil.copy2(entry, dest / entry.name)
            logging.info("Logs fetched for %s", state.name)
            return True, ""
        except Exception as exc:
            logging.exception("Failed log fetch for %s", state.name)
            return False, str(exc)

    def open_config_dialog(self, state: SignState) -> None:
        config_path = CONFIG_DIR / state.name / "config.json"
        config = read_config(CONFIG_DIR / state.name)
        dialog = ConfigDialog(state.name, config, self)
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
        ok, msg = self.is_share_reachable(state)
        if not ok:
            state.last_error = msg
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)
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
        except Exception as exc:
            state.last_error = str(exc)
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)

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

    def _on_column_active_toggle(self, sign_name: str, active: bool) -> None:
        state = self._get_state_by_sign_name(sign_name)
        if not state:
            return
        previous = state.enabled
        state.enabled = active
        self._save_inventory_state(state)
        if previous != active:
            before_label = "アクティブ" if previous else "非アクティブ"
            after_label = "アクティブ" if active else "非アクティブ"
            logging.info("[CMD] %s %s->%s", state.name, before_label, after_label)
        self._update_column(int(state.name.replace("Sign", "")) - 1, state)

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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
        force=True,
    )


def main():
    try:
        setup_logging()
    except Exception:
        pass

    try:
        app = QtWidgets.QApplication(sys.argv)
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
