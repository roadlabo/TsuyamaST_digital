"""Local PC Tkinter UI for RTSP-to-MP4 recording."""
from __future__ import annotations

import shutil
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path

from config.config_store import ConfigStore, CameraConfig
from recorder.recorder_manager import RecorderManager
from utils.logging_setup import setup_logging


def mask_rtsp(url: str) -> str:
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:****@{host}"
    return url


class LocalApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RTSP MP4録画システム（現地PC）")
        self.geometry("1280x760")
        self.store = ConfigStore()
        self.store.ensure_defaults()
        self.settings = self.store.load_settings()
        self.logger = setup_logging(self.settings["logs_dir"])
        self.cameras = self.store.load_cameras()
        self.manager = RecorderManager(self.cameras, self.settings, self.logger)
        self.manager.start_services()
        self.selected_id = tk.IntVar(value=1)
        self._build_ui()
        self._load_camera_to_form(1)
        self.after(1000, self._refresh_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        columns = ("id", "name", "enabled", "state", "last_file", "error")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=15)
        for col, text, width in [
            ("id", "ID", 40), ("name", "カメラ名", 160), ("enabled", "有効", 55),
            ("state", "状態", 90), ("last_file", "最終録画ファイル", 520), ("error", "最終エラー", 280),
        ]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="x", padx=10, pady=8)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        form = ttk.LabelFrame(self, text="カメラ設定")
        form.pack(fill="x", padx=10, pady=4)
        self.name_var = tk.StringVar()
        self.enabled_var = tk.BooleanVar()
        self.rtsp_var = tk.StringVar()
        self.subdir_var = tk.StringVar()
        self.segment_var = tk.IntVar(value=10)
        self.retention_var = tk.IntVar(value=30)
        fields = [("名前", self.name_var, 0), ("RTSP URL", self.rtsp_var, 1), ("保存サブdir", self.subdir_var, 2)]
        for label, var, row in fields:
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
            show = "*" if label == "RTSP URL" else ""
            ttk.Entry(form, textvariable=var, width=92, show=show).grid(row=row, column=1, columnspan=5, sticky="we", padx=6, pady=3)
        ttk.Checkbutton(form, text="有効", variable=self.enabled_var).grid(row=3, column=0, padx=6)
        ttk.Label(form, text="区切り(分)").grid(row=3, column=1, sticky="e")
        ttk.Spinbox(form, from_=1, to=120, textvariable=self.segment_var, width=8).grid(row=3, column=2, sticky="w")
        ttk.Label(form, text="保存日数").grid(row=3, column=3, sticky="e")
        ttk.Spinbox(form, from_=1, to=3650, textvariable=self.retention_var, width=8).grid(row=3, column=4, sticky="w")
        form.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=10, pady=6)
        for text, cmd in [
            ("設定読込", self._reload_config), ("設定保存", self._save_config), ("接続テスト", self._test_connection),
            ("全録画開始", self.manager.start_all), ("全録画停止", self.manager.stop_all),
            ("個別開始", lambda: self.manager.start_camera(self.selected_id.get())),
            ("個別停止", lambda: self.manager.stop_camera(self.selected_id.get())),
            ("全カメラMP4区切り", lambda: self.manager.split_all("UI手動区切り")),
            ("容量整理", self.manager.cleanup),
        ]:
            ttk.Button(buttons, text=text, command=cmd).pack(side="left", padx=4)

        self.disk_label = ttk.Label(self, text="空き容量: -")
        self.disk_label.pack(anchor="w", padx=10)
        self.log_text = tk.Text(self, height=12)
        self.log_text.pack(fill="both", expand=True, padx=10, pady=8)

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if sel:
            self._load_camera_to_form(int(sel[0]))

    def _load_camera_to_form(self, camera_id: int) -> None:
        cam = next(c for c in self.cameras if c.id == camera_id)
        self.selected_id.set(camera_id)
        self.name_var.set(cam.name)
        self.enabled_var.set(cam.enabled)
        self.rtsp_var.set(cam.rtsp_url)
        self.subdir_var.set(cam.save_subdir)
        self.segment_var.set(cam.segment_minutes)
        self.retention_var.set(cam.retention_days)

    def _save_current_form(self) -> None:
        idx = self.selected_id.get() - 1
        self.cameras[idx] = CameraConfig(
            id=self.selected_id.get(), name=self.name_var.get(), enabled=self.enabled_var.get(),
            rtsp_url=self.rtsp_var.get(), save_subdir=self.subdir_var.get() or f"cam{self.selected_id.get():02d}",
            segment_minutes=int(self.segment_var.get()), retention_days=int(self.retention_var.get()),
        )

    def _save_config(self) -> None:
        self._save_current_form()
        self.store.save_cameras(self.cameras)
        self._rebuild_manager()
        messagebox.showinfo("保存", "設定を保存し、録画管理へ反映しました。")
        self.logger.info("設定保存")

    def _reload_config(self) -> None:
        self.cameras = self.store.load_cameras()
        self._rebuild_manager()
        self._load_camera_to_form(self.selected_id.get())
        self.logger.info("設定読込")

    def _rebuild_manager(self) -> None:
        self.manager.stop_services()
        self.manager = RecorderManager(self.cameras, self.settings, self.logger)
        self.manager.start_services()

    def _test_connection(self) -> None:
        ok, msg = self.manager.test_camera(self.selected_id.get())
        messagebox.showinfo("接続テスト", ("成功: " if ok else "失敗: ") + msg)

    def _refresh_status(self) -> None:
        statuses = {s["id"]: s for s in self.manager.camera_statuses()}
        self.tree.delete(*self.tree.get_children())
        for cam in self.cameras:
            st = statuses.get(cam.id, {})
            self.tree.insert("", "end", iid=str(cam.id), values=(
                cam.id, cam.name, "ON" if cam.enabled else "OFF", st.get("recording_status", ""),
                st.get("last_completed_file", ""), st.get("last_error", ""),
            ))
        usage = shutil.disk_usage(self.settings["archive_dir"])
        self.disk_label.config(text=f"空き容量: {usage.free / (1024 ** 3):.1f} GB / Archive: {self.settings['archive_dir']}")
        log_path = Path(self.settings["logs_dir"]) / "app.log"
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", text)
        self.after(2000, self._refresh_status)

    def _on_close(self) -> None:
        self.manager.stop_services()
        self.destroy()


if __name__ == "__main__":
    LocalApp().mainloop()
