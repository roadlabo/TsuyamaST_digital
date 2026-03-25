from __future__ import annotations

from datetime import datetime
from typing import Any

import cv2
import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets


class ClickableImageLabel(QtWidgets.QLabel):
    point_clicked = QtCore.pyqtSignal(int, int)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self.point_clicked.emit(event.pos().x(), event.pos().y())
        super().mousePressEvent(event)


class CameraSettingsDialog(QtWidgets.QDialog):
    def __init__(self, camera_cfg: dict[str, Any], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"設定: {camera_cfg['camera_name']}")
        self.resize(900, 700)
        self.camera_cfg = camera_cfg
        self.line_points: list[list[int]] = [p[:] for p in camera_cfg.get("line_points", [])]
        self.exclude_polygon: list[list[int]] = [p[:] for p in camera_cfg.get("exclude_polygon", [])]
        self.mode = "line"

        root = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.name_edit = QtWidgets.QLineEdit(camera_cfg.get("camera_name", ""))
        self.url_edit = QtWidgets.QLineEdit(str(camera_cfg.get("stream_url", "")))
        self.th_edit = QtWidgets.QSpinBox()
        self.th_edit.setRange(0, 100)
        self.th_edit.setValue(int(camera_cfg.get("congestion_threshold", 60)))
        self.long_edit = QtWidgets.QSpinBox()
        self.long_edit.setRange(1, 180)
        self.long_edit.setValue(int(camera_cfg.get("long_stay_minutes", 15)))
        self.dir_combo = QtWidgets.QComboBox()
        self.dir_combo.addItems(["LtoR", "RtoL"])
        self.dir_combo.setCurrentText(camera_cfg.get("direction", "LtoR"))
        form.addRow("カメラ名", self.name_edit)
        form.addRow("stream_url", self.url_edit)
        form.addRow("渋滞閾値", self.th_edit)
        form.addRow("長時間滞在(分)", self.long_edit)
        form.addRow("方向", self.dir_combo)
        root.addLayout(form)

        self.image = ClickableImageLabel("snapshot")
        self.image.setMinimumHeight(320)
        self.image.setStyleSheet("background:#0c0f16;border:1px solid #00D7FF;")
        self.image.point_clicked.connect(self._on_click)
        root.addWidget(self.image)

        row = QtWidgets.QHBoxLayout()
        btn_snap = QtWidgets.QPushButton("静止画取得")
        btn_snap.clicked.connect(self._take_snapshot)
        btn_line = QtWidgets.QPushButton("ライン設定モード")
        btn_line.clicked.connect(lambda: self._set_mode("line"))
        btn_poly = QtWidgets.QPushButton("除外エリアモード")
        btn_poly.clicked.connect(lambda: self._set_mode("poly"))
        row.addWidget(btn_snap)
        row.addWidget(btn_line)
        row.addWidget(btn_poly)
        root.addLayout(row)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self.snapshot = None
        self._take_snapshot()

    def _set_mode(self, mode: str) -> None:
        self.mode = mode

    def _take_snapshot(self) -> None:
        src = self.url_edit.text().strip()
        cap = cv2.VideoCapture(int(src) if src.isdigit() else src)
        ok, frame = cap.read()
        cap.release()
        if not ok:
            frame = np.zeros((360, 640, 3), dtype=np.uint8)
            cv2.putText(frame, "snapshot failed", (30, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        self.snapshot = frame
        self._render_snapshot()

    def _on_click(self, x: int, y: int) -> None:
        if self.mode == "line":
            self.line_points.append([x, y])
            self.line_points = self.line_points[-2:]
        else:
            self.exclude_polygon.append([x, y])
        self._render_snapshot()

    def _render_snapshot(self) -> None:
        frame = self.snapshot.copy()
        if len(self.line_points) == 2:
            cv2.line(frame, tuple(self.line_points[0]), tuple(self.line_points[1]), (0, 255, 255), 2)
        if len(self.exclude_polygon) >= 2:
            cv2.polylines(frame, [np.array(self.exclude_polygon, np.int32)], False, (255, 0, 255), 2)
        h, w, _ = frame.shape
        qimg = QtGui.QImage(frame.data, w, h, frame.strides[0], QtGui.QImage.Format_BGR888)
        self.image.setPixmap(QtGui.QPixmap.fromImage(qimg).scaled(self.image.size(), QtCore.Qt.KeepAspectRatio))

    def get_updated_config(self) -> dict[str, Any]:
        cfg = dict(self.camera_cfg)
        cfg["camera_name"] = self.name_edit.text().strip() or cfg["camera_name"]
        cfg["stream_url"] = self.url_edit.text().strip()
        cfg["congestion_threshold"] = int(self.th_edit.value())
        cfg["long_stay_minutes"] = int(self.long_edit.value())
        cfg["direction"] = self.dir_combo.currentText()
        if len(self.line_points) == 2:
            cfg["line_points"] = self.line_points
        cfg["exclude_polygon"] = self.exclude_polygon
        return cfg


class CameraPanel(QtWidgets.QFrame):
    def __init__(self, camera_cfg: dict[str, Any], parent=None):
        super().__init__(parent)
        self.camera_id = camera_cfg["camera_id"]
        self.setStyleSheet(
            "QFrame{background:#0a0e13;border:1px solid #169db8;border-radius:6px;}"
            "QLabel{color:#cfefff;}"
        )
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        self.video = QtWidgets.QLabel("video")
        self.video.setMinimumSize(540, 280)
        self.video.setStyleSheet("background:#010203;border:1px solid #00a6d6;")
        root.addWidget(self.video, 3)

        right = QtWidgets.QVBoxLayout()
        self.title = QtWidgets.QLabel(camera_cfg["camera_name"])
        self.title.setStyleSheet("font-size:15px;color:#00D7FF;font-weight:bold;")
        right.addWidget(self.title)

        self.meter = QtWidgets.QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setFormat("Congestion %p")
        self.meter.setStyleSheet(
            "QProgressBar{background:#0f1822;border:1px solid #00d7ff;color:white;}"
            "QProgressBar::chunk{background:#00d7ff;}"
        )
        right.addWidget(self.meter)

        self.meta = QtWidgets.QLabel("time / gpu / fps")
        right.addWidget(self.meta)

        self.hist = QtWidgets.QLabel("Histogram prev/today")
        self.hist.setMinimumHeight(120)
        self.hist.setStyleSheet("background:#0f1620;border:1px solid #1d6f8b;")
        right.addWidget(self.hist)

        self.long_stay = QtWidgets.QTextEdit()
        self.long_stay.setReadOnly(True)
        self.long_stay.setMinimumHeight(90)
        self.long_stay.setStyleSheet("background:#0f1620;border:1px solid #1d6f8b;color:#ffaeae;")
        right.addWidget(self.long_stay)

        root.addLayout(right, 2)

    def update_view(self, payload: dict[str, Any]) -> None:
        frame = payload.get("frame")
        if frame is not None:
            h, w, _ = frame.shape
            qimg = QtGui.QImage(frame.data, w, h, frame.strides[0], QtGui.QImage.Format_BGR888)
            self.video.setPixmap(QtGui.QPixmap.fromImage(qimg).scaled(self.video.size(), QtCore.Qt.KeepAspectRatio))

        score = int(payload.get("congestion_score", 0))
        threshold = int(payload.get("threshold", 60))
        self.meter.setValue(score)
        if score >= threshold:
            self.meter.setStyleSheet(
                "QProgressBar{background:#281111;border:1px solid #ffde59;color:white;}"
                "QProgressBar::chunk{background:#ff1c1c;}"
            )
        else:
            self.meter.setStyleSheet(
                "QProgressBar{background:#0f1822;border:1px solid #ffde59;color:white;}"
                "QProgressBar::chunk{background:#00d7ff;}"
            )

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.meta.setText(
            f"{now} | device={payload.get('device')} | GPU={payload.get('gpu_name')} | FPS={payload.get('fps',0):.1f} | TH={threshold}"
        )
        self.hist.setText(self._hist_text(payload.get("hist_prev", []), payload.get("hist_today", [])))
        lines = [f"ID {tid}: {mins:.1f} min" for tid, mins in payload.get("long_stays", [])]
        self.long_stay.setText("\n".join(lines) if lines else "No long stay")

    @staticmethod
    def _hist_text(prev: list[int], today: list[int]) -> str:
        pairs = []
        for i in range(0, 144, 24):
            p = sum(prev[i:i+24]) if prev else 0
            t = sum(today[i:i+24]) if today else 0
            hour = i // 6
            pairs.append(f"{hour:02d}:00 Prev={p} / Today={t}")
        return "\n".join(pairs)
