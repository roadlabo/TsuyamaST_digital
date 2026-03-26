from __future__ import annotations

from datetime import datetime
from typing import Any

import cv2
import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets

CLASS_MAP = {
    0: "人",
    1: "自転車",
    2: "車",
    3: "オートバイ",
    4: "飛行機",
    5: "バス",
    6: "電車",
    7: "トラック",
}


class ClickableImageLabel(QtWidgets.QLabel):
    point_clicked = QtCore.pyqtSignal(int, int)

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.source_size = (1, 1)

    def set_source_size(self, width: int, height: int) -> None:
        self.source_size = (max(1, width), max(1, height))

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        pix = self.pixmap()
        if pix is None:
            return
        scaled = pix.size()
        off_x = (self.width() - scaled.width()) // 2
        off_y = (self.height() - scaled.height()) // 2
        local_x = event.pos().x() - off_x
        local_y = event.pos().y() - off_y
        if local_x < 0 or local_y < 0 or local_x >= scaled.width() or local_y >= scaled.height():
            return
        src_w, src_h = self.source_size
        x = int(local_x * src_w / max(1, scaled.width()))
        y = int(local_y * src_h / max(1, scaled.height()))
        self.point_clicked.emit(x, y)
        super().mousePressEvent(event)


def _segments_intersect(p1, p2, p3, p4) -> bool:
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])

    return ccw(p1, p3, p4) != ccw(p2, p3, p4) and ccw(p1, p2, p3) != ccw(p1, p2, p4)


def has_self_intersection(points: list[list[int]]) -> bool:
    if len(points) < 4:
        return False
    n = len(points)
    for i in range(n):
        a1 = points[i]
        a2 = points[(i + 1) % n]
        for j in range(i + 1, n):
            if abs(i - j) <= 1 or (i == 0 and j == n - 1):
                continue
            b1 = points[j]
            b2 = points[(j + 1) % n]
            if _segments_intersect(a1, a2, b1, b2):
                return True
    return False


class CameraSettingsDialog(QtWidgets.QDialog):
    def __init__(self, camera_cfg: dict[str, Any], latest_frame=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"設定: {camera_cfg['camera_name']}")
        self.resize(1000, 780)
        self.camera_cfg = dict(camera_cfg)
        self.line_points = [camera_cfg.get("line_start", [100, 100])[:], camera_cfg.get("line_end", [400, 100])[:]]
        self.exclude_polygon: list[list[int]] = [p[:] for p in camera_cfg.get("exclude_polygon", [])]
        self.mode = "line"

        root = QtWidgets.QVBoxLayout(self)

        tabs = QtWidgets.QTabWidget()
        root.addWidget(tabs)

        basic = QtWidgets.QWidget()
        tabs.addTab(basic, "基本")
        form = QtWidgets.QFormLayout(basic)
        self.name_edit = QtWidgets.QLineEdit(camera_cfg.get("camera_name", ""))
        self.url_edit = QtWidgets.QLineEdit(str(camera_cfg.get("stream_url", "")))
        self.dir_combo = QtWidgets.QComboBox()
        self.dir_combo.addItems(["line_vector", "legacy_x"])
        self.dir_combo.setCurrentText(camera_cfg.get("line_direction_mode", "line_vector"))

        form.addRow("カメラ名", self.name_edit)
        form.addRow("stream_url", self.url_edit)
        form.addRow("line direction mode", self.dir_combo)

        ai_tab = QtWidgets.QWidget()
        tabs.addTab(ai_tab, "解析条件")
        ai_form = QtWidgets.QFormLayout(ai_tab)

        self.yolo_model = QtWidgets.QLineEdit(str(camera_cfg.get("yolo_model", "yolo11n.pt")))
        self.conf = QtWidgets.QDoubleSpinBox(); self.conf.setRange(0, 1); self.conf.setSingleStep(0.01); self.conf.setValue(float(camera_cfg.get("confidence_threshold", 0.25)))
        self.iou = QtWidgets.QDoubleSpinBox(); self.iou.setRange(0, 1); self.iou.setSingleStep(0.01); self.iou.setValue(float(camera_cfg.get("iou_threshold", 0.5)))
        self.frame_skip = QtWidgets.QSpinBox(); self.frame_skip.setRange(1, 30); self.frame_skip.setValue(int(camera_cfg.get("frame_skip", 1)))
        self.imgsz = QtWidgets.QSpinBox(); self.imgsz.setRange(320, 2048); self.imgsz.setSingleStep(32); self.imgsz.setValue(int(camera_cfg.get("imgsz", 640)))
        self.bt_hi = QtWidgets.QDoubleSpinBox(); self.bt_hi.setRange(0, 1); self.bt_hi.setValue(float(camera_cfg.get("bt_track_high_thresh", 0.3)))
        self.bt_lo = QtWidgets.QDoubleSpinBox(); self.bt_lo.setRange(0, 1); self.bt_lo.setValue(float(camera_cfg.get("bt_track_low_thresh", 0.1)))
        self.bt_match = QtWidgets.QDoubleSpinBox(); self.bt_match.setRange(0, 1); self.bt_match.setValue(float(camera_cfg.get("bt_match_thresh", 0.8)))
        self.bt_buffer = QtWidgets.QSpinBox(); self.bt_buffer.setRange(1, 1000); self.bt_buffer.setValue(int(camera_cfg.get("bt_track_buffer", 30)))
        self.crossing = QtWidgets.QLineEdit(str(camera_cfg.get("crossing_judgment_pattern", "line_cross")))
        self.distance_th = QtWidgets.QDoubleSpinBox(); self.distance_th.setRange(1, 1000); self.distance_th.setValue(float(camera_cfg.get("distance_threshold", 25.0)))
        self.cong_interval = QtWidgets.QSpinBox(); self.cong_interval.setRange(1, 60); self.cong_interval.setValue(int(camera_cfg.get("congestion_calculation_interval", 10)))
        self.enable_cong = QtWidgets.QCheckBox("enable_congestion"); self.enable_cong.setChecked(bool(camera_cfg.get("enable_congestion", True)))

        ai_form.addRow("yolo_model", self.yolo_model)
        ai_form.addRow("confidence_threshold", self.conf)
        ai_form.addRow("iou_threshold", self.iou)
        ai_form.addRow("frame_skip", self.frame_skip)
        ai_form.addRow("imgsz", self.imgsz)
        ai_form.addRow("bt_track_high_thresh", self.bt_hi)
        ai_form.addRow("bt_track_low_thresh", self.bt_lo)
        ai_form.addRow("bt_match_thresh", self.bt_match)
        ai_form.addRow("bt_track_buffer", self.bt_buffer)
        ai_form.addRow("crossing_judgment_pattern", self.crossing)
        ai_form.addRow("distance_threshold", self.distance_th)
        ai_form.addRow("congestion_calculation_interval", self.cong_interval)
        ai_form.addRow(self.enable_cong)

        class_group = QtWidgets.QGroupBox("対象クラス (0-7)")
        class_layout = QtWidgets.QGridLayout(class_group)
        self.class_checks: dict[int, QtWidgets.QCheckBox] = {}
        selected = set(int(x) for x in camera_cfg.get("target_classes", [2, 3, 5, 7]))
        for i in range(8):
            cb = QtWidgets.QCheckBox(f"{CLASS_MAP[i]}({i})")
            cb.setChecked(i in selected)
            self.class_checks[i] = cb
            class_layout.addWidget(cb, i // 4, i % 4)
        ai_form.addRow(class_group)

        self.image = ClickableImageLabel("snapshot")
        self.image.setMinimumHeight(340)
        self.image.setStyleSheet("background:#0c0f16;border:1px solid #00D7FF;")
        self.image.point_clicked.connect(self._on_click)
        root.addWidget(self.image)

        row = QtWidgets.QHBoxLayout()
        btn_line = QtWidgets.QPushButton("ライン設定")
        btn_line.clicked.connect(lambda: self._set_mode("line"))
        btn_poly = QtWidgets.QPushButton("除外エリア設定")
        btn_poly.clicked.connect(lambda: self._set_mode("poly"))
        btn_finish_poly = QtWidgets.QPushButton("指定終了")
        btn_finish_poly.clicked.connect(self._finish_polygon)
        btn_reset = QtWidgets.QPushButton("やり直し")
        btn_reset.clicked.connect(self._reset_mode)
        row.addWidget(btn_line); row.addWidget(btn_poly); row.addWidget(btn_finish_poly); row.addWidget(btn_reset)
        root.addLayout(row)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._validate_and_accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self.snapshot = latest_frame
        if self.snapshot is None:
            self.snapshot = np.zeros((360, 640, 3), dtype=np.uint8)
            cv2.putText(self.snapshot, "No live frame", (30, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        self._render_snapshot()

    def _set_mode(self, mode: str) -> None:
        self.mode = mode

    def _on_click(self, x: int, y: int) -> None:
        if self.mode == "line":
            if len(self.line_points) >= 2:
                self.line_points = []
            self.line_points.append([x, y])
        else:
            self.exclude_polygon.append([x, y])
        self._render_snapshot()

    def _finish_polygon(self) -> None:
        if len(self.exclude_polygon) < 3:
            QtWidgets.QMessageBox.warning(self, "警告", "除外エリアは3点以上必要です。")
            return
        if has_self_intersection(self.exclude_polygon):
            QtWidgets.QMessageBox.warning(self, "警告", "自己交差ポリゴンは設定できません。やり直してください。")
            self.exclude_polygon = []
        self._render_snapshot()

    def _reset_mode(self) -> None:
        if self.mode == "line":
            self.line_points = []
        else:
            self.exclude_polygon = []
        self._render_snapshot()

    def _render_snapshot(self) -> None:
        frame = self.snapshot.copy()
        if len(self.line_points) == 2:
            cv2.line(frame, tuple(self.line_points[0]), tuple(self.line_points[1]), (0, 255, 255), 2)
            for p in self.line_points:
                cv2.circle(frame, tuple(p), 4, (0, 255, 255), -1)
                cv2.putText(frame, f"{p[0]},{p[1]}", (p[0] + 5, p[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        for i, p in enumerate(self.exclude_polygon):
            cv2.circle(frame, tuple(p), 4, (255, 0, 255), -1)
            cv2.putText(frame, str(i + 1), (p[0] + 5, p[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        if len(self.exclude_polygon) >= 2:
            cv2.polylines(frame, [np.array(self.exclude_polygon, np.int32)], len(self.exclude_polygon) >= 3, (255, 0, 255), 2)

        h, w, _ = frame.shape
        self.image.set_source_size(w, h)
        qimg = QtGui.QImage(frame.data, w, h, frame.strides[0], QtGui.QImage.Format.Format_BGR888)
        self.image.setPixmap(QtGui.QPixmap.fromImage(qimg).scaled(self.image.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio))

    def _validate_and_accept(self) -> None:
        selected_classes = [i for i, cb in self.class_checks.items() if cb.isChecked()]
        if not selected_classes:
            QtWidgets.QMessageBox.warning(self, "警告", "対象クラスを1つ以上選択してください。")
            return
        if len(self.line_points) != 2:
            QtWidgets.QMessageBox.warning(self, "警告", "ラインは2点指定してください。")
            return
        self.accept()

    def get_updated_config(self) -> dict[str, Any]:
        cfg = dict(self.camera_cfg)
        cfg["camera_name"] = self.name_edit.text().strip() or cfg["camera_name"]
        cfg["stream_url"] = self.url_edit.text().strip()
        cfg["line_start"] = self.line_points[0]
        cfg["line_end"] = self.line_points[1]
        cfg["exclude_polygon"] = self.exclude_polygon
        cfg["line_direction_mode"] = self.dir_combo.currentText()
        cfg["yolo_model"] = self.yolo_model.text().strip() or "yolo11n.pt"
        cfg["confidence_threshold"] = float(self.conf.value())
        cfg["iou_threshold"] = float(self.iou.value())
        cfg["frame_skip"] = int(self.frame_skip.value())
        cfg["imgsz"] = int(self.imgsz.value())
        cfg["target_classes"] = [i for i, cb in self.class_checks.items() if cb.isChecked()]
        cfg["bt_track_high_thresh"] = float(self.bt_hi.value())
        cfg["bt_track_low_thresh"] = float(self.bt_lo.value())
        cfg["bt_match_thresh"] = float(self.bt_match.value())
        cfg["bt_track_buffer"] = int(self.bt_buffer.value())
        cfg["crossing_judgment_pattern"] = self.crossing.text().strip() or "line_cross"
        cfg["distance_threshold"] = float(self.distance_th.value())
        cfg["congestion_calculation_interval"] = int(self.cong_interval.value())
        cfg["enable_congestion"] = bool(self.enable_cong.isChecked())
        return cfg


class TimeSeriesGraph(QtWidgets.QLabel):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.setMinimumHeight(140)
        self.setStyleSheet("background:#0f1620;border:1px solid #1d6f8b;color:#cfefff;")

    def draw_line_series(self, points: list[tuple[datetime, float]]) -> None:
        w, h = max(300, self.width()), max(120, self.height())
        pix = QtGui.QPixmap(w, h)
        pix.fill(QtGui.QColor("#0f1620"))
        painter = QtGui.QPainter(pix)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        margin = 28
        plot = QtCore.QRectF(margin, 16, w - margin - 10, h - 40)
        painter.setPen(QtGui.QPen(QtGui.QColor("#35516b"), 1))
        painter.drawRect(plot)
        painter.setPen(QtGui.QColor("#cfefff"))
        painter.drawText(8, 14, self.title)

        if points:
            ys = [v for _, v in points]
            ymin, ymax = min(ys), max(ys)
            if abs(ymax - ymin) < 1e-9:
                ymax = ymin + 1.0
            path = QtGui.QPainterPath()
            for i, (ts, value) in enumerate(points):
                sec = ts.hour * 3600 + ts.minute * 60 + ts.second
                x = plot.left() + (sec / 86400.0) * plot.width()
                y = plot.bottom() - ((value - ymin) / (ymax - ymin)) * plot.height()
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            painter.setPen(QtGui.QPen(QtGui.QColor("#00D7FF"), 2))
            painter.drawPath(path)
        painter.drawText(int(plot.left()), h - 10, "0:00")
        painter.drawText(int(plot.right()) - 40, h - 10, "24:00")
        painter.end()
        self.setPixmap(pix)

    def draw_bars(self, ltor: list[int], rtol: list[int]) -> None:
        w, h = max(300, self.width()), max(120, self.height())
        pix = QtGui.QPixmap(w, h)
        pix.fill(QtGui.QColor("#0f1620"))
        painter = QtGui.QPainter(pix)
        margin = 28
        plot = QtCore.QRectF(margin, 16, w - margin - 10, h - 40)
        painter.setPen(QtGui.QPen(QtGui.QColor("#35516b"), 1))
        painter.drawRect(plot)
        painter.setPen(QtGui.QColor("#cfefff"))
        painter.drawText(8, 14, self.title)

        maxv = max(1, max(ltor) if ltor else 0, max(rtol) if rtol else 0)
        bin_w = plot.width() / 144.0
        for i in range(144):
            l = ltor[i] if i < len(ltor) else 0
            r = rtol[i] if i < len(rtol) else 0
            x0 = plot.left() + i * bin_w
            h_l = (l / maxv) * plot.height()
            h_r = (r / maxv) * plot.height()
            painter.fillRect(QtCore.QRectF(x0, plot.bottom() - h_l, bin_w * 0.45, h_l), QtGui.QColor("#00D7FF"))
            painter.fillRect(QtCore.QRectF(x0 + bin_w * 0.5, plot.bottom() - h_r, bin_w * 0.45, h_r), QtGui.QColor("#ff8f66"))

        painter.drawText(int(plot.left()), h - 10, "0:00")
        painter.drawText(int(plot.right()) - 40, h - 10, "24:00")
        painter.end()
        self.setPixmap(pix)


class CameraPanel(QtWidgets.QFrame):
    def __init__(self, camera_cfg: dict[str, Any], parent=None):
        super().__init__(parent)
        self.camera_id = camera_cfg["camera_id"]
        self.setStyleSheet("QFrame{background:#0a0e13;border:1px solid #169db8;border-radius:6px;} QLabel{color:#cfefff;}")
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
        right.addWidget(self.meter)

        self.meta = QtWidgets.QLabel("time / gpu / fps")
        right.addWidget(self.meta)

        self.summary = QtWidgets.QLabel("LtoR=0 / RtoL=0")
        right.addWidget(self.summary)

        self.congestion_graph = TimeSeriesGraph("渋滞指数(10秒更新)")
        right.addWidget(self.congestion_graph)
        self.pass_graph = TimeSeriesGraph("通過台数(台/10分) LtoR/RtoL")
        right.addWidget(self.pass_graph)

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
            qimg = QtGui.QImage(frame.data, w, h, frame.strides[0], QtGui.QImage.Format.Format_BGR888)
            self.video.setPixmap(QtGui.QPixmap.fromImage(qimg).scaled(self.video.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio))

        score = int(payload.get("congestion_score", 0))
        threshold = int(payload.get("threshold", 60))
        self.meter.setValue(max(0, min(100, score)))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.meta.setText(f"{now} | device={payload.get('device')} | GPU={payload.get('gpu_name')} | FPS={payload.get('fps',0):.1f} | TH={threshold}")

        ltor = payload.get("pass_bins_ltor", [0] * 144)
        rtol = payload.get("pass_bins_rtol", [0] * 144)
        self.summary.setText(f"LtoR 合計={sum(ltor)} / RtoL 合計={sum(rtol)}")
        self.congestion_graph.draw_line_series(payload.get("congestion_points", []))
        self.pass_graph.draw_bars(ltor, rtol)

        lines = [f"ID {tid}: {mins:.1f} min" for tid, mins in payload.get("long_stays", [])]
        self.long_stay.setText("\n".join(lines) if lines else "No long stay")
