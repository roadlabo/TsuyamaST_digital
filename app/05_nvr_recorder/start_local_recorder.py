"""Local PC Tkinter UI for RTSP-to-MP4 recording."""
from __future__ import annotations

import shutil
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from config.config_store import CameraConfig, ConfigStore, build_dir_settings
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
        self.geometry("1280x840")
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
        storage = ttk.LabelFrame(self, text="保存先設定")
        storage.pack(fill="x", padx=10, pady=8)
        self.system_dir_var = tk.StringVar(value=self.settings.get("system_dir", "D:/NVR"))
        self.storage_dir_var = tk.StringVar(value=self.settings.get("storage_dir", "D:/NVR"))
        ttk.Label(storage, text="一時・ログ・状態フォルダ(内蔵SSD)").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(storage, textvariable=self.system_dir_var, width=88).grid(row=0, column=1, sticky="we", padx=6, pady=3)
        ttk.Button(storage, text="参照...", command=self._browse_system_dir).grid(row=0, column=2, padx=6, pady=3)
        ttk.Label(storage, text="完成MP4保存フォルダ(外付けHDD)").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(storage, textvariable=self.storage_dir_var, width=88).grid(row=1, column=1, sticky="we", padx=6, pady=3)
        ttk.Button(storage, text="参照...", command=self._browse_storage_dir).grid(row=1, column=2, padx=6, pady=3)
        ttk.Button(storage, text="保存先を適用", command=self._apply_storage_dirs).grid(row=0, column=3, rowspan=2, padx=6, pady=3)
        self.cleanup_note = ttk.Label(storage, text="HDDの空き容量が約5TBを下回ると、完成MP4を古いものから自動削除します。日数による削除は行いません。")
        self.cleanup_note.grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=3)
        storage.columnconfigure(1, weight=1)

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
        fields = [("名前", self.name_var, 0), ("RTSP URL", self.rtsp_var, 1), ("保存サブdir", self.subdir_var, 2)]
        for label, var, row in fields:
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=3)
            show = "*" if label == "RTSP URL" else ""
            ttk.Entry(form, textvariable=var, width=92, show=show).grid(row=row, column=1, columnspan=5, sticky="we", padx=6, pady=3)
        ttk.Checkbutton(form, text="有効", variable=self.enabled_var).grid(row=3, column=0, padx=6)
        ttk.Label(form, text="区切り(分)").grid(row=3, column=1, sticky="e")
        ttk.Spinbox(form, from_=1, to=120, textvariable=self.segment_var, width=8).grid(row=3, column=2, sticky="w")
        ttk.Label(form, text="保存期間は日数ではなく、HDD空き容量で管理します").grid(row=3, column=3, columnspan=3, sticky="w", padx=6)
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
        self.path_label = ttk.Label(self, text="")
        self.path_label.pack(anchor="w", padx=10)
        self.log_text = tk.Text(self, height=12)
        self.log_text.pack(fill="both", expand=True, padx=10, pady=8)

    def _browse_system_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.system_dir_var.get() or "D:/", title="一時・ログ・状態フォルダを選択")
        if selected:
            self.system_dir_var.set(selected)

    def _browse_storage_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.storage_dir_var.get() or "D:/", title="完成MP4保存フォルダを選択")
        if selected:
            self.storage_dir_var.set(selected)

    def _apply_storage_dirs(self) -> None:
        system_dir = self.system_dir_var.get().strip()
        storage_dir = self.storage_dir_var.get().strip()
        if not system_dir or not storage_dir:
            messagebox.showwarning("保存先", "一時フォルダと保存フォルダを両方入力してください。")
            return
        if self.manager.system_status() == "running":
            if not messagebox.askyesno("保存先変更", "録画管理を一度停止して保存先を変更します。よろしいですか？"):
                return
        self._save_current_form()
        self.manager.stop_services()
        self.settings.update(build_dir_settings(system_dir, storage_dir))
        self.settings["min_free_gb"] = 5120
        self.store.save_settings(self.settings)
        self.store.save_cameras(self.cameras)
        self.logger = setup_logging(self.settings["logs_dir"])
        self._rebuild_manager()
        messagebox.showinfo("保存先", f"保存先を変更しました。\n一時: {self.settings['system_dir']}\n完成MP4: {self.settings['archive_dir']}")
        self.logger.info("保存先変更: system=%s storage=%s", self.settings["system_dir"], self.settings["storage_dir"])

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

    def _save_current_form(self) -> None:
        idx = self.selected_id.get() - 1
        self.cameras[idx] = CameraConfig(
            id=self.selected_id.get(), name=self.name_var.get(), enabled=self.enabled_var.get(),
            rtsp_url=self.rtsp_var.get(), save_subdir=self.subdir_var.get() or f"cam{self.selected_id.get():02d}",
            segment_minutes=int(self.segment_var.get()), retention_days=30,
        )

    def _save_config(self) -> None:
        self._save_current_form()
        self.settings["min_free_gb"] = 5120
        self.store.save_settings(self.settings)
        self.store.save_cameras(self.cameras)
        self._rebuild_manager()
        messagebox.showinfo("保存", "設定を保存し、録画管理へ反映しました。")
        self.logger.info("設定保存")

    def _reload_config(self) -> None:
        self.settings = self.store.load_settings()
        self.system_dir_var.set(self.settings.get("system_dir", "D:/NVR"))
        self.storage_dir_var.set(self.settings.get("storage_dir", "D:/NVR"))
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
        try:
            usage = shutil.disk_usage(self.settings["archive_dir"])
            min_free = float(self.settings.get("min_free_gb", 5120))
            self.disk_label.config(text=f"HDD空き容量: {usage.free / (1024 ** 4):.2f} TB / 全体: {usage.total / (1024 ** 4):.2f} TB / 自動削除閾値: {min_free / 1024:.1f} TB")
        except OSError as exc:
            self.disk_label.config(text=f"HDD空き容量: 確認できません ({exc})")
        self.path_label.config(text=f"一時: {self.settings['temp_dir']} / 完成MP4: {self.settings['archive_dir']}")
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
