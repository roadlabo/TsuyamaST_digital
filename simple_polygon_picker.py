import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

Point = Tuple[int, int]


def format_points_as_csv(points: List[Point]) -> str:
    """ポイント列をCSV風テキストへ整形する。"""
    return "\n".join(f"{x},{y}" for x, y in points)


def format_points_as_json(points: List[Point]) -> str:
    """ポイント列をJSON/Python風テキストへ整形する。"""
    return json.dumps([[x, y] for x, y in points], ensure_ascii=False)


class ImageCanvas(QLabel):
    """画像表示、座標変換、ポリゴン描画を担当するキャンバス。"""

    point_added = pyqtSignal(int, int)
    points_changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(800, 600)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.image_path: Optional[Path] = None
        self.original_pixmap: Optional[QPixmap] = None
        self.points: List[Point] = []
        self.closed_polygon: bool = False

        # 表示中画像の実際の矩形（レターボックス補正用）
        self._display_rect = (0, 0, 0, 0)  # x, y, w, h

    def load_image(self, path: str) -> bool:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return False

        self.image_path = Path(path)
        self.original_pixmap = pixmap
        self.points.clear()
        self.update_overlay()
        self.points_changed.emit()
        return True

    def set_closed(self, is_closed: bool) -> None:
        self.closed_polygon = is_closed
        self.update_overlay()

    def clear_points(self) -> None:
        self.points.clear()
        self.update_overlay()
        self.points_changed.emit()

    def set_points(self, points: List[Point]) -> None:
        self.points = list(points)
        self.update_overlay()
        self.points_changed.emit()

    def undo_last_point(self) -> bool:
        if not self.points:
            return False
        self.points.pop()
        self.update_overlay()
        self.points_changed.emit()
        return True

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt命名)
        super().resizeEvent(event)
        self.update_overlay()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.original_pixmap is None:
            return

        if event.button() == Qt.MouseButton.LeftButton:
            image_point = self.widget_to_image_pos(event.position())
            if image_point is None:
                return
            self.points.append(image_point)
            self.update_overlay()
            self.point_added.emit(image_point[0], image_point[1])
            self.points_changed.emit()
        elif event.button() == Qt.MouseButton.RightButton:
            # 追加要望: 右クリックで1点戻す
            self.undo_last_point()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.original_pixmap is not None
            and len(self.points) >= 3
        ):
            # 追加要望: ダブルクリックで閉じる扱い
            self.closed_polygon = True
            self.update_overlay()
            self.points_changed.emit()
            return
        super().mouseDoubleClickEvent(event)

    def widget_to_image_pos(self, pos: QPointF) -> Optional[Point]:
        """ウィジェット座標を元画像ピクセル座標へ変換する。"""
        if self.original_pixmap is None:
            return None

        dx, dy, dw, dh = self._display_rect
        if dw <= 0 or dh <= 0:
            return None

        x = pos.x()
        y = pos.y()

        # レターボックス範囲外は無効
        if not (dx <= x < dx + dw and dy <= y < dy + dh):
            return None

        rel_x = (x - dx) / dw
        rel_y = (y - dy) / dh

        img_w = self.original_pixmap.width()
        img_h = self.original_pixmap.height()

        img_x = int(max(0, min(img_w - 1, round(rel_x * (img_w - 1)))))
        img_y = int(max(0, min(img_h - 1, round(rel_y * (img_h - 1)))))
        return img_x, img_y

    def update_overlay(self) -> None:
        """元画像 + ポリゴン描画を毎回再生成して表示する。"""
        if self.original_pixmap is None:
            self._display_rect = (0, 0, 0, 0)
            self.clear()
            self.setText("画像を読み込んでください")
            return

        # 元画像サイズ上で描画（座標の整合を保つ）
        annotated = self.original_pixmap.copy()
        painter = QPainter(annotated)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 線描画
        if len(self.points) >= 2:
            line_pen = QPen(QColor(40, 220, 40), 2)
            painter.setPen(line_pen)
            for i in range(len(self.points) - 1):
                p1 = self.points[i]
                p2 = self.points[i + 1]
                painter.drawLine(p1[0], p1[1], p2[0], p2[1])

        # 閉じ線描画
        if self.closed_polygon and len(self.points) >= 3:
            close_pen = QPen(QColor(80, 160, 255), 2)
            painter.setPen(close_pen)
            p_last = self.points[-1]
            p_first = self.points[0]
            painter.drawLine(p_last[0], p_last[1], p_first[0], p_first[1])

        # 点 + 番号描画
        point_pen = QPen(QColor(255, 70, 70), 2)
        label_pen = QPen(QColor(255, 230, 80), 1)
        painter.setFont(QFont("Sans Serif", 12))

        for idx, (x, y) in enumerate(self.points, start=1):
            painter.setPen(point_pen)
            painter.setBrush(QColor(255, 70, 70))
            painter.drawEllipse(x - 4, y - 4, 8, 8)

            painter.setPen(label_pen)
            painter.drawText(x + 6, y - 6, str(idx))

        painter.end()

        # QLabel全体に背景を作って中央配置（余白を正確に保持）
        frame = QPixmap(self.size())
        frame.fill(QColor(24, 24, 24))

        scaled = annotated.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        offset_x = (self.width() - scaled.width()) // 2
        offset_y = (self.height() - scaled.height()) // 2
        self._display_rect = (offset_x, offset_y, scaled.width(), scaled.height())

        frame_painter = QPainter(frame)
        frame_painter.drawPixmap(offset_x, offset_y, scaled)
        frame_painter.end()

        self.setPixmap(frame)


class MainWindow(QMainWindow):
    """メインUIとボタン処理を担当するウィンドウ。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Simple Polygon Picker")
        self.resize(1400, 900)

        self.canvas = ImageCanvas(self)
        self.canvas.point_added.connect(self.on_canvas_point_added)
        self.canvas.points_changed.connect(self.on_points_changed)

        central = QWidget(self)
        self.setCentralWidget(central)

        root_layout = QHBoxLayout(central)
        root_layout.addWidget(self.canvas, stretch=1)

        panel = QWidget(self)
        panel.setFixedWidth(350)
        panel_layout = QVBoxLayout(panel)

        self.btn_open = QPushButton("画像を開く")
        self.btn_auto_detect = QPushButton("自動検出")
        self.btn_undo = QPushButton("1点戻す")
        self.btn_clear = QPushButton("クリア")
        self.chk_closed = QCheckBox("閉じる/開く")

        self.lbl_latest = QLabel("最新座標: -")
        self.lbl_count = QLabel("点数: 0")
        self.lbl_bbox = QLabel("BBox: -")

        self.text_points = QPlainTextEdit()
        mono_font = QFont("Consolas", 11)
        if not mono_font.exactMatch():
            mono_font = QFont("Monospace", 11)
        self.text_points.setFont(mono_font)
        self.text_points.setPlaceholderText("# polygon_points\n120,85\n250,90")

        self.lbl_json_preview = QLabel("JSON形式: []")
        self.lbl_json_preview.setWordWrap(True)

        self.btn_save = QPushButton("テキスト保存")

        for widget in (
            self.btn_open,
            self.btn_auto_detect,
            self.btn_undo,
            self.btn_clear,
            self.chk_closed,
            self.lbl_latest,
            self.lbl_count,
            self.lbl_bbox,
            self.text_points,
            self.lbl_json_preview,
            self.btn_save,
        ):
            panel_layout.addWidget(widget)

        panel_layout.addStretch(1)
        root_layout.addWidget(panel)

        # シグナル接続
        self.btn_open.clicked.connect(self.open_image)
        self.btn_auto_detect.clicked.connect(self.auto_detect_polygon)
        self.btn_undo.clicked.connect(self.undo_last_point)
        self.btn_clear.clicked.connect(self.clear_all)
        self.chk_closed.toggled.connect(self.toggle_closed)
        self.btn_save.clicked.connect(self.save_text)

        self.update_text_output()

    @property
    def points(self) -> List[Point]:
        return self.canvas.points

    def open_image(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "画像を開く",
            "",
            "Image Files (*.png *.jpg *.jpeg *.bmp)",
        )
        if not file_path:
            return

        if not self.canvas.load_image(file_path):
            QMessageBox.warning(self, "読込エラー", "画像を読み込めませんでした。")
            return

        self.chk_closed.setChecked(False)
        self.lbl_latest.setText("最新座標: -")
        self.on_points_changed()

    def on_canvas_point_added(self, x: int, y: int) -> None:
        self.lbl_latest.setText(f"最新座標: x={x}, y={y}")

    def on_points_changed(self) -> None:
        self.update_status_labels()
        self.update_text_output()

    def update_status_labels(self) -> None:
        self.lbl_count.setText(f"点数: {len(self.points)}")
        if not self.points:
            self.lbl_bbox.setText("BBox: -")
            if self.lbl_latest.text().startswith("最新座標:") is False:
                self.lbl_latest.setText("最新座標: -")
            return

        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        self.lbl_bbox.setText(
            f"BBox: xmin={min(xs)}, ymin={min(ys)}, xmax={max(xs)}, ymax={max(ys)}"
        )

    def update_text_output(self) -> None:
        csv_body = format_points_as_csv(self.points)
        if csv_body:
            content = "# polygon_points\n" + csv_body
        else:
            content = "# polygon_points"
        self.text_points.setPlainText(content)
        self.lbl_json_preview.setText(f"JSON形式: {format_points_as_json(self.points)}")

    def save_text(self) -> None:
        if not self.points:
            QMessageBox.warning(self, "保存不可", "保存する点がありません。")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "テキスト保存",
            "polygon_points.txt",
            "Text Files (*.txt);;CSV Files (*.csv)",
        )
        if not file_path:
            return

        try:
            Path(file_path).write_text(self.text_points.toPlainText(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "保存エラー", f"保存に失敗しました:\n{exc}")
            return

        QMessageBox.information(self, "保存完了", f"保存しました:\n{file_path}")

    def clear_all(self) -> None:
        # 画像は残して点のみ初期化
        self.canvas.clear_points()
        self.lbl_latest.setText("最新座標: -")

    def undo_last_point(self) -> None:
        if not self.canvas.undo_last_point():
            QMessageBox.information(self, "情報", "削除できる点がありません。")
            return

        if self.points:
            x, y = self.points[-1]
            self.lbl_latest.setText(f"最新座標: x={x}, y={y}")
        else:
            self.lbl_latest.setText("最新座標: -")

    def toggle_closed(self, checked: bool) -> None:
        self.canvas.set_closed(checked)

    def auto_detect_polygon(self) -> None:
        if self.canvas.original_pixmap is None or self.canvas.image_path is None:
            QMessageBox.warning(self, "自動検出", "先に画像を読み込んでください。")
            return

        try:
            import cv2  # type: ignore
        except ImportError:
            QMessageBox.information(
                self,
                "自動検出",
                "OpenCV が見つからないため未実装です。\n手動クリックで頂点を指定してください。",
            )
            return

        image = cv2.imread(str(self.canvas.image_path))
        if image is None:
            QMessageBox.warning(self, "自動検出", "画像の読み込みに失敗しました。")
            return

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(gray, 60, 160)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if not contours:
            QMessageBox.information(self, "自動検出", "輪郭を検出できませんでした。")
            return

        largest = max(contours, key=cv2.contourArea)
        epsilon = 0.01 * cv2.arcLength(largest, True)
        approx = cv2.approxPolyDP(largest, epsilon, True)

        detected: List[Point] = [(int(p[0][0]), int(p[0][1])) for p in approx]
        if len(detected) < 3:
            QMessageBox.information(self, "自動検出", "十分な頂点を検出できませんでした。")
            return

        self.canvas.set_points(detected)
        self.chk_closed.setChecked(True)
        x, y = detected[-1]
        self.lbl_latest.setText(f"最新座標: x={x}, y={y}")
        QMessageBox.information(self, "自動検出", f"{len(detected)} 点を検出しました。")


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
