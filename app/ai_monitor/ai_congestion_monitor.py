from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from modules.camera_worker import CameraWorker
from modules.config_manager import ConfigManager
from modules.plot_utils import save_multi_day_trend_plot
from modules.report_writer import ReportWriter
from modules.status_manager import StatusManager
from modules.ui_panels import CameraPanel, CameraSettingsDialog


class MonitorMainWindow(QtWidgets.QMainWindow):
    def __init__(self, root_dir: Path):
        super().__init__()
        self.root_dir = root_dir
        self.setWindowTitle("AI Congestion Monitor")
        self.setStyleSheet("background:#02060a;")

        self.cfg_mgr = ConfigManager(root_dir)
        self.app_cfg = self.cfg_mgr.load()
        self.reporter = ReportWriter(root_dir / "data")
        ai_status_path = Path(self.app_cfg.system.get("ai_status_json_path", "app/config/ai_status.json"))
        if not ai_status_path.is_absolute():
            ai_status_path = Path.cwd() / ai_status_path
        self.status_mgr = StatusManager(ai_status_path)

        self.workers: dict[int, CameraWorker] = {}
        self.panels: dict[int, CameraPanel] = {}

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        toolbar = QtWidgets.QHBoxLayout()
        btn_setting = QtWidgets.QPushButton("設定")
        btn_setting.clicked.connect(self.open_settings)
        btn_daily = QtWidgets.QPushButton("日次Excel出力")
        btn_daily.clicked.connect(self.export_daily)
        btn_monthly = QtWidgets.QPushButton("月次Excel出力")
        btn_monthly.clicked.connect(self.export_monthly)
        toolbar.addWidget(btn_setting)
        toolbar.addWidget(btn_daily)
        toolbar.addWidget(btn_monthly)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        for cam in self.app_cfg.cameras:
            if not cam.get("enabled", True):
                continue
            panel = CameraPanel(cam)
            layout.addWidget(panel, 1)
            self.panels[cam["camera_id"]] = panel
            self.workers[cam["camera_id"]] = CameraWorker(cam, self.app_cfg.system, self.root_dir)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(self.app_cfg.system.get("ui_refresh_interval_ms", 500)))

        self.setWindowState(QtCore.Qt.WindowMaximized)

    def tick(self) -> None:
        payloads = {}
        for cid, worker in self.workers.items():
            try:
                p = worker.process_once()
                payloads[cid] = p
                self.panels[cid].update_view(p)
            except Exception as exc:
                print(f"[WARN] camera {cid} failed: {exc}")

        cam1 = payloads.get(1, {})
        cam2 = payloads.get(2, {})
        cam3 = payloads.get(3, {})
        cam2_cfg = next((c for c in self.app_cfg.cameras if c.get("camera_id") == 2), {})
        level = self.status_mgr.decide_level(
            cam1_over=bool(cam1.get("threshold_over", False)),
            cam2_long_stay_count=int(cam2.get("long_stay_count", 0)),
            cam2_long_stay_trigger_count=int(cam2_cfg.get("long_stay_trigger_count", 1)),
            cam3_over=bool(cam3.get("threshold_over", False)),
        )
        self.status_mgr.update_if_needed(level)

    def open_settings(self) -> None:
        cams = self.app_cfg.cameras
        names = [f"{c['camera_id']}: {c['camera_name']}" for c in cams]
        selected, ok = QtWidgets.QInputDialog.getItem(self, "対象カメラ", "設定するカメラ", names, 0, False)
        if not ok:
            return
        camera_id = int(selected.split(":", 1)[0])
        idx = next(i for i, c in enumerate(cams) if c["camera_id"] == camera_id)
        dlg = CameraSettingsDialog(cams[idx], self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            cams[idx] = dlg.get_updated_config()
            self.cfg_mgr.save_camera_settings(cams)
            QtWidgets.QMessageBox.information(self, "保存", "設定を保存しました。再起動で完全反映されます。")

    def export_daily(self) -> None:
        path = self.reporter.write_daily_report(date.today(), self.app_cfg.cameras, self.root_dir / "data" / "metrics")
        self._export_multi_day_plot()
        QtWidgets.QMessageBox.information(self, "日次", f"出力完了: {path}")

    def export_monthly(self) -> None:
        month = datetime.now().strftime("%Y-%m")
        path = self.reporter.write_monthly_report(month, self.root_dir / "data" / "metrics", self.app_cfg.cameras)
        QtWidgets.QMessageBox.information(self, "月次", f"出力完了: {path}")

    def _export_multi_day_plot(self) -> None:
        metrics_files = []
        for cam in self.app_cfg.cameras:
            cid = cam["camera_id"]
            cam_dir = self.root_dir / "data" / "metrics" / f"cam{cid}"
            metrics_files.extend(sorted(cam_dir.glob("realtime_metrics_*.csv"))[-7:])
        out = self.root_dir / "data" / "reports" / "daily" / f"multi_day_trend_{date.today().isoformat()}.png"
        save_multi_day_trend_plot(metrics_files, "congestion_score", out)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="3-camera AI congestion monitor")
    p.add_argument("--root", default=str(Path(__file__).resolve().parent), help="ai_monitor root directory")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    root_dir = Path(args.root)
    app = QtWidgets.QApplication(sys.argv)
    win = MonitorMainWindow(root_dir)
    win.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
