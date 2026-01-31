import json
import importlib.util
import logging
import os
import shutil
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

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
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

INVENTORY_PATH = CONFIG_DIR / "inventory.json"
AI_STATUS_PATH = CONFIG_DIR / "ai_status.json"
SETTINGS_PATH = CONFIG_DIR / "controller_settings.json"

CHANNELS = [f"ch{idx:02d}" for idx in range(1, 21)]
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
ROW_LABEL_WIDTH = 180
COLUMN_WIDTH = 140


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
    last_distribute_ok: bool = True


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


def parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


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
        if key in ai_channels:
            return ai_channels[key]

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


class ConfigDialog(QtWidgets.QDialog):
    def __init__(self, sign_name: str, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{sign_name} 設定")
        self.config = config

        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()

        self.enabled_checkbox = QtWidgets.QCheckBox("有効")
        self.enabled_checkbox.setChecked(config.get("enabled", True))
        form.addRow("有効/無効", self.enabled_checkbox)

        self.sleep_combo = QtWidgets.QComboBox()
        self.sleep_combo.addItems(CHANNELS)
        self.sleep_combo.setCurrentText(config.get("sleep_channel", "ch01"))
        form.addRow("休眠チャンネル", self.sleep_combo)

        self.normal_combo = QtWidgets.QComboBox()
        self.normal_combo.addItems(CHANNELS)
        self.normal_combo.setCurrentText(config.get("normal_channel", "ch05"))
        form.addRow("通常チャンネル", self.normal_combo)

        self.ai_level2 = QtWidgets.QComboBox()
        self.ai_level2.addItems(CHANNELS)
        self.ai_level2.setCurrentText(config.get("ai_channels", {}).get("level2", "ch02"))
        form.addRow("AI LV2", self.ai_level2)

        self.ai_level3 = QtWidgets.QComboBox()
        self.ai_level3.addItems(CHANNELS)
        self.ai_level3.setCurrentText(config.get("ai_channels", {}).get("level3", "ch03"))
        form.addRow("AI LV3", self.ai_level3)

        self.ai_level4 = QtWidgets.QComboBox()
        self.ai_level4.addItems(CHANNELS)
        self.ai_level4.setCurrentText(config.get("ai_channels", {}).get("level4", "ch04"))
        form.addRow("AI LV4", self.ai_level4)

        layout.addLayout(form)

        layout.addWidget(QtWidgets.QLabel("休眠時間帯"))
        self.sleep_table = QtWidgets.QTableWidget(0, 2)
        self.sleep_table.setHorizontalHeaderLabels(["開始", "終了"])
        self.sleep_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.sleep_table)

        for rule in config.get("sleep_rules", []):
            self.add_sleep_rule(rule)

        sleep_buttons = QtWidgets.QHBoxLayout()
        sleep_add = QtWidgets.QPushButton("追加")
        sleep_remove = QtWidgets.QPushButton("削除")
        sleep_buttons.addWidget(sleep_add)
        sleep_buttons.addWidget(sleep_remove)
        layout.addLayout(sleep_buttons)

        self.timer_table = QtWidgets.QTableWidget(0, 3)
        self.timer_table.setHorizontalHeaderLabels(["開始", "終了", "CH"])
        self.timer_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
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

        add_button.clicked.connect(lambda: self.add_timer_rule({"start": "00:00", "end": "00:00", "channel": "ch01"}))
        remove_button.clicked.connect(self.remove_selected_rule)
        sleep_add.clicked.connect(lambda: self.add_sleep_rule({"start": "00:00", "end": "00:00"}))
        sleep_remove.clicked.connect(self.remove_selected_sleep_rule)

        action_layout = QtWidgets.QHBoxLayout()
        save_button = QtWidgets.QPushButton("保存")
        cancel_button = QtWidgets.QPushButton("キャンセル")
        action_layout.addStretch()
        action_layout.addWidget(save_button)
        action_layout.addWidget(cancel_button)
        layout.addLayout(action_layout)

        save_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)

    def add_timer_rule(self, rule: dict) -> None:
        row = self.timer_table.rowCount()
        self.timer_table.insertRow(row)
        self.timer_table.setItem(row, 0, QtWidgets.QTableWidgetItem(rule.get("start", "")))
        self.timer_table.setItem(row, 1, QtWidgets.QTableWidgetItem(rule.get("end", "")))
        self.timer_table.setItem(row, 2, QtWidgets.QTableWidgetItem(rule.get("channel", "")))

    def add_sleep_rule(self, rule: dict) -> None:
        row = self.sleep_table.rowCount()
        self.sleep_table.insertRow(row)
        self.sleep_table.setItem(row, 0, QtWidgets.QTableWidgetItem(rule.get("start", "")))
        self.sleep_table.setItem(row, 1, QtWidgets.QTableWidgetItem(rule.get("end", "")))

    def remove_selected_rule(self) -> None:
        row = self.timer_table.currentRow()
        if row >= 0:
            self.timer_table.removeRow(row)

    def remove_selected_sleep_rule(self) -> None:
        row = self.sleep_table.currentRow()
        if row >= 0:
            self.sleep_table.removeRow(row)

    def build_config(self) -> dict:
        timer_rules = []
        for row in range(self.timer_table.rowCount()):
            start_item = self.timer_table.item(row, 0)
            end_item = self.timer_table.item(row, 1)
            channel_item = self.timer_table.item(row, 2)
            timer_rules.append(
                {
                    "start": start_item.text() if start_item else "",
                    "end": end_item.text() if end_item else "",
                    "channel": channel_item.text() if channel_item else "",
                }
            )
        sleep_rules = []
        for row in range(self.sleep_table.rowCount()):
            start_item = self.sleep_table.item(row, 0)
            end_item = self.sleep_table.item(row, 1)
            sleep_rules.append(
                {
                    "start": start_item.text() if start_item else "",
                    "end": end_item.text() if end_item else "",
                }
            )
        return {
            "enabled": self.enabled_checkbox.isChecked(),
            "sleep_channel": self.sleep_combo.currentText(),
            "sleep_rules": sleep_rules,
            "normal_channel": self.normal_combo.currentText(),
            "ai_channels": {
                "level2": self.ai_level2.currentText(),
                "level3": self.ai_level3.currentText(),
                "level4": self.ai_level4.currentText(),
            },
            "timer_rules": timer_rules,
        }


class EmittingStream(QtCore.QObject):
    text_written = QtCore.Signal(str)

    def write(self, text):
        if text:
            self.text_written.emit(text)

    def flush(self):
        return None


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
        self.setMinimumWidth(ROW_LABEL_WIDTH)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor("white"))
        painter.setPen(QtGui.QPen(QtGui.QColor(80, 80, 80)))
        height = self.height()
        for hour in [0, 6, 12, 18, 23]:
            y = int(height * (hour * 60) / (24 * 60))
            painter.drawLine(0, y, 20, y)
            painter.drawText(25, y + 4, f"{hour:02d}:00")

        legend_top = height - 110
        x = 10
        y = legend_top
        for idx, (channel, color) in enumerate(TIMER_CHANNEL_COLORS.items()):
            painter.fillRect(x, y, 12, 12, color)
            painter.drawRect(x, y, 12, 12)
            painter.drawText(x + 18, y + 11, channel)
            y += 16
            if (idx + 1) % 5 == 0:
                y = legend_top
                x += 70


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
    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.setFixedWidth(COLUMN_WIDTH)
        self.layout = QtWidgets.QVBoxLayout(self)
        self.layout.setContentsMargins(4, 2, 4, 2)
        self.layout.setSpacing(2)

        self.no_label = self._make_label(name.replace("Signage", ""))
        self.display_label = self._make_label("-")
        self.ai_label = self._make_label("-")
        self.preview_label = QtWidgets.QLabel("サンプルなし")
        self.preview_label.setAlignment(QtCore.Qt.AlignCenter)
        self.preview_label.setFixedHeight(120)
        self.setting_button = QtWidgets.QPushButton("変更")
        self.sleep_label = self._make_label("-")
        self.ai_lv2_label = self._make_label("-")
        self.ai_lv3_label = self._make_label("-")
        self.ai_lv4_label = self._make_label("-")
        self.normal_label = self._make_label("-")
        self.timer_bar = TimerBarWidget()
        self.timer_bar.setFixedHeight(280)

        self.power_widget = QtWidgets.QWidget()
        power_layout = QtWidgets.QVBoxLayout(self.power_widget)
        power_layout.setContentsMargins(0, 0, 0, 0)
        power_layout.setSpacing(2)
        self.btn_reboot = QtWidgets.QPushButton("再起動")
        self.btn_shutdown = QtWidgets.QPushButton("シャットダウン")
        power_layout.addWidget(self.btn_reboot)
        power_layout.addWidget(self.btn_shutdown)

        self.manage_widget = QtWidgets.QWidget()
        manage_layout = QtWidgets.QVBoxLayout(self.manage_widget)
        manage_layout.setContentsMargins(0, 0, 0, 0)
        manage_layout.setSpacing(2)
        self.manage_label = self._make_label("アクティブ")
        self.btn_resend = QtWidgets.QPushButton("再送")
        self.btn_resend.setObjectName("resend_button")
        manage_layout.addWidget(self.manage_label)
        manage_layout.addWidget(self.btn_resend)

        for widget, height in [
            (self.no_label, 28),
            (self.display_label, 48),
            (self.ai_label, 40),
            (self.preview_label, 120),
            (self.setting_button, 36),
            (self.sleep_label, 32),
            (self.ai_lv2_label, 28),
            (self.ai_lv3_label, 28),
            (self.ai_lv4_label, 28),
            (self.normal_label, 32),
            (self.timer_bar, 280),
            (self.power_widget, 60),
            (self.manage_widget, 44),
        ]:
            widget.setFixedHeight(height)
            self.layout.addWidget(widget)

        self.layout.addStretch()

    def _make_label(self, text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet("border: 1px solid #999;")
        return label

    def set_inactive_style(self, inactive: bool) -> None:
        if inactive:
            self.setStyleSheet("background-color: #c9c9c9; color: #7a7a7a;")
        else:
            self.setStyleSheet("")
        self.timer_bar.set_column_enabled(not inactive)


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
        self._header_labels: Dict[str, QtWidgets.QLabel] = {}
        self._column_widgets: Dict[str, SignageColumnWidget] = {}

        self._init_ui()
        self._setup_log_stream()
        self._load_sign_states()
        self.refresh_summary()
        self.start_watchers()

        self.timer_poll = QtCore.QTimer(self)
        self.timer_poll.setInterval(60 * 1000)
        self.timer_poll.timeout.connect(self.check_timer_transition)
        self.timer_poll.start()

    def _init_ui(self) -> None:
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)

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
        layout.addLayout(header_layout)

        header_row = QtWidgets.QHBoxLayout()
        left_header = QtWidgets.QLabel("")
        left_header.setFixedWidth(ROW_LABEL_WIDTH)
        header_row.addWidget(left_header)

        self.header_scroll = QtWidgets.QScrollArea()
        self.header_scroll.setWidgetResizable(True)
        self.header_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.header_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        header_container = QtWidgets.QWidget()
        header_container_layout = QtWidgets.QHBoxLayout(header_container)
        header_container_layout.setContentsMargins(0, 0, 0, 0)
        header_container_layout.setSpacing(2)

        for idx in range(1, 21):
            name = f"Signage {idx:02d}"
            label = QtWidgets.QLabel(name)
            label.setAlignment(QtCore.Qt.AlignCenter)
            label.setFixedWidth(COLUMN_WIDTH)
            label.setStyleSheet("border: 1px solid #999;")
            header_container_layout.addWidget(label)
            self._header_labels[name.replace("Signage ", "Signage")] = label

        header_container_layout.addStretch()
        self.header_scroll.setWidget(header_container)
        header_row.addWidget(self.header_scroll)
        layout.addLayout(header_row)

        body_layout = QtWidgets.QHBoxLayout()
        left_column = QtWidgets.QWidget()
        left_column.setFixedWidth(ROW_LABEL_WIDTH)
        left_layout = QtWidgets.QVBoxLayout(left_column)
        left_layout.setContentsMargins(2, 2, 2, 2)
        left_layout.setSpacing(2)

        left_layout.addWidget(self._make_row_label("番号", 28))
        left_layout.addWidget(self._make_row_label("表示中\nch", 48))
        left_layout.addWidget(self._make_row_label("AI渋滞判定", 40))
        left_layout.addWidget(self._make_row_label("表示中映像", 120))
        left_layout.addWidget(self._make_row_label("設定", 36))
        left_layout.addWidget(self._make_row_label("休眠時", 32))
        left_layout.addWidget(self._make_row_label("AI渋滞判定時\nLV2", 28))
        left_layout.addWidget(self._make_row_label("AI渋滞判定時\nLV3", 28))
        left_layout.addWidget(self._make_row_label("AI渋滞判定時\nLV4", 28))
        left_layout.addWidget(self._make_row_label("通常時", 32))
        timer_label = TimerLegendWidget()
        timer_label.setFixedHeight(280)
        left_layout.addWidget(timer_label)
        left_layout.addWidget(self._make_row_label("サイネージPC\n電源管理", 60))
        left_layout.addWidget(self._make_row_label("管理する\nサイネージ", 44))
        left_layout.addStretch()

        body_layout.addWidget(left_column)

        self.body_scroll = QtWidgets.QScrollArea()
        self.body_scroll.setWidgetResizable(True)
        body_container = QtWidgets.QWidget()
        body_container_layout = QtWidgets.QHBoxLayout(body_container)
        body_container_layout.setContentsMargins(0, 0, 0, 0)
        body_container_layout.setSpacing(2)

        for idx in range(1, 21):
            name = f"Signage{idx:02d}"
            column = SignageColumnWidget(name)
            body_container_layout.addWidget(column)
            self._column_widgets[name] = column

        body_container_layout.addStretch()
        self.body_scroll.setWidget(body_container)
        body_layout.addWidget(self.body_scroll)
        layout.addLayout(body_layout)

        log_label = QtWidgets.QLabel("ログ")
        layout.addWidget(log_label)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setStyleSheet("background-color: #000; color: #fff;")
        layout.addWidget(self.log_view, stretch=1)

        self.setCentralWidget(central)

        self.btn_check.clicked.connect(self.check_connectivity)
        self.btn_bulk_update.clicked.connect(self.bulk_update)
        self.btn_refresh_content.clicked.connect(self.refresh_preview_info)
        self.btn_sync.clicked.connect(self.start_sync)
        self.btn_logs.clicked.connect(self.collect_logs)
        self.btn_preview_toggle.clicked.connect(self.toggle_preview)

        self.body_scroll.horizontalScrollBar().valueChanged.connect(
            self.header_scroll.horizontalScrollBar().setValue
        )
        self.header_scroll.horizontalScrollBar().valueChanged.connect(
            self.body_scroll.horizontalScrollBar().setValue
        )

    def _make_row_label(self, text: str, height: int) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setFixedHeight(height)
        label.setStyleSheet("border: 1px solid #999;")
        label.setWordWrap(True)
        return label

    def _setup_log_stream(self) -> None:
        self._log_stream = EmittingStream()
        self._log_stream.text_written.connect(self.append_log_text)
        sys.stdout = self._log_stream
        sys.stderr = self._log_stream
        handler = LogHandler(self._log_stream)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        self._log_handler = handler

        def excepthook(exc_type, exc_value, exc_traceback):
            formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
            self._log_stream.write(formatted)

        sys.excepthook = excepthook

    def append_log_text(self, text: str) -> None:
        self.log_view.moveCursor(QtGui.QTextCursor.End)
        self.log_view.insertPlainText(text)
        self.log_view.moveCursor(QtGui.QTextCursor.End)

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
            config = read_config(CONFIG_DIR / name)
            state.enabled = config.get("enabled", True)
            self.sign_states[name] = state

    def refresh_summary(self) -> None:
        for col, (name, state) in enumerate(sorted(self.sign_states.items())):
            self._update_column(col, state)

    def _update_column(self, col: int, state: SignState) -> None:
        column = self._column_widgets.get(state.name.replace("Sign", "Signage"))
        if not column:
            return

        config = read_config(CONFIG_DIR / state.name)
        column.display_label.setText(state.active_channel or "-")
        column.ai_label.setText(self.build_ai_text())
        column.sleep_label.setText(config.get("sleep_channel", "ch01"))
        column.ai_lv2_label.setText(config.get("ai_channels", {}).get("level2", "ch02"))
        column.ai_lv3_label.setText(config.get("ai_channels", {}).get("level3", "ch03"))
        column.ai_lv4_label.setText(config.get("ai_channels", {}).get("level4", "ch04"))
        column.normal_label.setText(config.get("normal_channel", "ch05"))
        column.timer_bar.set_rules(config.get("timer_rules", []))

        column.setting_button.clicked.disconnect() if column.setting_button.receivers(column.setting_button.clicked) else None
        column.btn_reboot.clicked.disconnect() if column.btn_reboot.receivers(column.btn_reboot.clicked) else None
        column.btn_shutdown.clicked.disconnect() if column.btn_shutdown.receivers(column.btn_shutdown.clicked) else None
        column.btn_resend.clicked.disconnect() if column.btn_resend.receivers(column.btn_resend.clicked) else None

        column.setting_button.clicked.connect(lambda checked=False, s=state: self.open_config_dialog(s))
        column.btn_reboot.clicked.connect(lambda checked=False, s=state: self.send_power_command(s, "reboot"))
        column.btn_shutdown.clicked.connect(lambda checked=False, s=state: self.send_power_command(s, "shutdown"))
        column.btn_resend.clicked.connect(lambda checked=False, s=state: self.resend_active(s))

        inactive = (not state.exists) or (not state.enabled)
        column.manage_label.setText("非アクティブ" if inactive else "アクティブ")
        column.set_inactive_style(inactive)
        for btn in [column.setting_button, column.btn_reboot, column.btn_shutdown, column.btn_resend]:
            btn.setEnabled(state.exists and state.enabled)

        header_label = self._header_labels.get(state.name.replace("Sign", "Signage"))
        if header_label:
            if inactive:
                header_label.setStyleSheet("border: 1px solid #999; background-color: #c9c9c9; color: #7a7a7a;")
            else:
                header_label.setStyleSheet("border: 1px solid #999;")

        self.update_preview_cell(state, column.preview_label)
        self.update_resend_button(state)

    def update_resend_button(self, state: SignState) -> None:
        column = self._column_widgets.get(state.name.replace("Sign", "Signage"))
        if column:
            column.btn_resend.setEnabled(state.exists and state.enabled and not state.last_distribute_ok)

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

    def toggle_preview(self) -> None:
        self._preview_enabled = not self._preview_enabled
        self.refresh_preview_info()

    def refresh_preview_info(self) -> None:
        for state in self.sign_states.values():
            column = self._column_widgets.get(state.name.replace("Sign", "Signage"))
            if column:
                self.update_preview_cell(state, column.preview_label)

    def update_preview_cell(self, state: SignState, label: QtWidgets.QLabel) -> None:
        label.clear()
        if not state.exists or not state.enabled:
            label.setText("非アクティブ")
            return
        if not self._preview_enabled or not state.active_channel:
            label.setText("プレビューOFF")
            return

        sample = self.find_sample_file(state.active_channel)
        if not sample:
            label.setText("サンプルなし")
            return

        if cv2 is None:
            label.setText(f"サンプル: {sample.name}")
            return

        frame = self.read_sample_frame(sample)
        if frame is None:
            label.setText(f"サンプル: {sample.name}")
            return

        height, width, _ = frame.shape
        image = QtGui.QImage(frame.data, width, height, QtGui.QImage.Format_BGR888)
        pixmap = QtGui.QPixmap.fromImage(image).scaled(200, 120, QtCore.Qt.KeepAspectRatio)
        label.setPixmap(pixmap)

    def find_sample_file(self, channel: str) -> Optional[Path]:
        path = CONTENT_DIR / channel
        if not path.exists():
            return None
        for entry in path.iterdir():
            if entry.is_file() and entry.name.endswith("_sample.mp4"):
                return entry
        return None

    def read_sample_frame(self, file_path: Path):
        capture = cv2.VideoCapture(str(file_path))
        ok, frame = capture.read()
        capture.release()
        if not ok:
            return None
        return frame

    def check_connectivity(self) -> None:
        timeout = self.settings.get("network_timeout_seconds", 4)
        futures = {}
        for state in self.sign_states.values():
            if not state.exists or not state.enabled:
                continue
            futures[self._executor.submit(self.check_single_connectivity, state)] = state

        for future, state in futures.items():
            try:
                online, error = future.result(timeout=timeout)
                state.online = online
                state.last_error = error or ""
                state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            except Exception as exc:
                state.online = False
                state.last_error = str(exc)
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)

    def check_single_connectivity(self, state: SignState) -> Tuple[bool, str]:
        remote_path = build_unc_path(state.ip, state.share_name, "app\\config")
        try:
            return Path(remote_path).exists(), ""
        except Exception as exc:
            return False, str(exc)

    def recompute_all(self) -> None:
        with self._update_lock:
            self.ai_status = load_json(AI_STATUS_PATH, self.ai_status)
            now = datetime.now()
            updated_any = False
            for state in self.sign_states.values():
                config = read_config(CONFIG_DIR / state.name)
                state.enabled = config.get("enabled", True)
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
        logging.info("Bulk update triggered")
        self.recompute_all()
        self.distribute_all()

    def distribute_all(self) -> None:
        timeout = self.settings.get("network_timeout_seconds", 4)
        futures = {}
        for state in self.sign_states.values():
            if not state.exists or not state.enabled:
                continue
            futures[self._executor.submit(self.distribute_active, state)] = state

        for future, state in futures.items():
            try:
                ok, message = future.result(timeout=timeout)
                state.last_distribute_ok = ok
                state.last_error = message if not ok else ""
                state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            except Exception as exc:
                state.last_distribute_ok = False
                state.last_error = str(exc)
            self.update_resend_button(state)
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)

    def distribute_active(self, state: SignState) -> Tuple[bool, str]:
        active = read_active(CONFIG_DIR / state.name)
        if not active.get("active_channel"):
            return False, "active_channel missing"
        remote_path = build_unc_path(state.ip, state.share_name, "app\\config\\active.json")
        try:
            write_json_atomic(Path(remote_path), active)
            logging.info("Distributed active.json to %s", state.name)
            return True, ""
        except Exception as exc:
            logging.exception("Failed to distribute to %s", state.name)
            return False, str(exc)

    def resend_active(self, state: SignState) -> None:
        future = self._executor.submit(self.distribute_active, state)

        def _complete(fut):
            try:
                ok, message = fut.result()
                state.last_distribute_ok = ok
                state.last_error = message if not ok else ""
            except Exception as exc:
                state.last_distribute_ok = False
                state.last_error = str(exc)
            state.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.update_resend_button(state)
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)

        future.add_done_callback(lambda fut: QtCore.QTimer.singleShot(0, lambda: _complete(fut)))

    def start_sync(self) -> None:
        logging.info("Starting content sync")
        timeout = self.settings.get("network_timeout_seconds", 4)
        futures = {}
        for state in self.sign_states.values():
            if not state.exists or not state.enabled:
                continue
            futures[self._executor.submit(self.sync_sign_content, state)] = state
        for future, state in futures.items():
            try:
                ok, message = future.result(timeout=timeout)
                state.last_error = message if not ok else ""
            except Exception as exc:
                state.last_error = str(exc)
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)

    def sync_sign_content(self, state: SignState) -> Tuple[bool, str]:
        staging_base = self.settings.get("sync_staging_subdir", "staging\\sync_tmp")
        for channel in CHANNELS:
            local_dir = CONTENT_DIR / channel
            if not local_dir.exists():
                continue
            remote_staging = build_unc_path(state.ip, state.share_name, f"{staging_base}\\{channel}")
            remote_content = build_unc_path(state.ip, state.share_name, f"content\\{channel}")
            ensure_dir(Path(remote_staging))
            ensure_dir(Path(remote_content))
            for entry in local_dir.iterdir():
                if entry.is_dir():
                    continue
                remote_file = Path(remote_content) / entry.name
                if self.file_needs_sync(entry, remote_file):
                    shutil.copy2(entry, Path(remote_staging) / entry.name)
                    shutil.copy2(Path(remote_staging) / entry.name, remote_file)
        logging.info("Content sync completed for %s", state.name)
        return True, ""

    def file_needs_sync(self, local_file: Path, remote_file: Path) -> bool:
        if not remote_file.exists():
            return True
        try:
            local_stat = local_file.stat()
            remote_stat = remote_file.stat()
        except Exception:
            return True
        return local_stat.st_size != remote_stat.st_size or int(local_stat.st_mtime) != int(remote_stat.st_mtime)

    def collect_logs(self) -> None:
        timeout = self.settings.get("network_timeout_seconds", 4)
        futures = {}
        for state in self.sign_states.values():
            if not state.exists or not state.enabled:
                continue
            futures[self._executor.submit(self.fetch_logs_for_sign, state)] = state
        for future, state in futures.items():
            try:
                ok, message = future.result(timeout=timeout)
                state.last_error = message if not ok else ""
            except Exception as exc:
                state.last_error = str(exc)
            self._update_column(int(state.name.replace("Sign", "")) - 1, state)

    def fetch_logs_for_sign(self, state: SignState) -> Tuple[bool, str]:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_root = Path(self.settings.get("log_backup_dir", str(ROOT_DIR.parent / "backup" / "logs")))
        dest = backup_root / state.name / timestamp
        ensure_dir(dest)
        remote_logs = build_unc_path(state.ip, state.share_name, "app\\logs")
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
            new_config = dialog.build_config()
            write_json_atomic(config_path, new_config)
            logging.info("Config saved for %s", state.name)
            self.recompute_all()

    def send_power_command(self, state: SignState, command: str) -> None:
        confirm = QtWidgets.QMessageBox.question(
            self,
            "確認",
            f"{state.name} を {command} しますか？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        command_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{state.name}"
        payload = {
            "command_id": command_id,
            "command": command,
            "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        remote_path = build_unc_path(state.ip, state.share_name, "app\\config\\command.json")
        try:
            write_json_atomic(Path(remote_path), payload)
            logging.info("Power command %s sent to %s", command, state.name)
        except Exception as exc:
            state.last_error = str(exc)
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
    )


def main():
    setup_logging()
    app = QtWidgets.QApplication(sys.argv)
    window = ControllerWindow()
    window.showMaximized()
    window.recompute_all()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
