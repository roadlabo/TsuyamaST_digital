"""Local PC Tkinter UI for RTSP-to-MP4 recording."""
from __future__ import annotations

import ctypes
import os
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


def status_icon(state: str) -> str:
    if state == "録画中":
        return "● 録画中"
    if state == "区切り中":
        return "● 区切り中"
    if state == "再接続中":
        return "▲ 再接続中"
    if state == "エラー":
        return "× エラー"
    if state in ("停止中", "無効", "未設定"):
        return f"■ {state}"
    if state == "接続確認中":
        return "● 接続確認中"
    return state or "■ 不明"


def status_tag(state: str) -> str:
    if state == "録画中":
        return "recording"
    if state in ("区切り中", "接続確認中"):
        return "working"
    if state == "再接続中":
        return "warning"
    if state == "エラー":
        return "error"
    return "stopped"


def iter_windows_drives() -> list[str]:
    if os.name != "nt":
        return []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    drives: list[str] = []
    for index in range(26):
        if mask & (1 << index):
            drives.append(f"{chr(65 + index)}:/")
    return drives


def unique_drive_roots(*paths: str) -> list[str]:
    roots = iter_windows_drives()
    if roots:
        return roots
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        root = Path(raw).anchor or str(Path(raw).resolve().anchor)
        if root and root not in seen:
            seen.add(root)
    return sorted(seen)


def newest_first_log_text(text: str, max_lines: int = 250) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    return "\n".join(reversed(lines[-max_lines:]))


class LocalApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RTSP MP4録画システム（現地PC）")
        self.geometry("1360x930")
        self.store = ConfigStore()
        self.store.ensure_defaults()
        self.settings = self.store.load_settings()
        self.logger = setup_logging(self.settings["logs_dir"])
        self.cameras = self.store.load_cameras()
        self.manager = RecorderManager(self.cameras, self.settings, self.logger)
        self.manager.start_services()
        self.selected_id = tk.IntVar(value=1)
        self.drive_widgets: dict[str, dict[str, object]] = {}
        self._build_ui()
        self._load_camera_to_form(1)
        self._show_operation("● 待機中", "idle")
        self.after(1000, self._refresh_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure("Title.TLabel", font=("Yu Gothic UI", 10, "bold"))
        style.configure("Action.TLabel", font=("Yu Gothic UI", 11, "bold"))
        style.configure("Help.TLabel", foreground="#555555")

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

        dashboard = ttk.LabelFrame(self, text="状態ダッシュボード")
        dashboard.pack(fill="x", padx=10, pady=4)
        self.operation_label = ttk.Label(dashboard, text="● 待機中", style="Action.TLabel")
        self.operation_label.grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.path_label = ttk.Label(dashboard, text="")
        self.path_label.grid(row=0, column=1, sticky="w", padx=12, pady=4)
        dashboard.columnconfigure(1, weight=1)

        drives = ttk.LabelFrame(self, text="ドライブ容量（全ドライブ）")
        drives.pack(fill="x", padx=10, pady=4)
        self.drives_frame = drives
        self._build_drive_rows()

        columns = ("id", "name", "enabled", "state", "last_file", "error")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=12)
        for col, text, width in [
            ("id", "ID", 40), ("name", "カメラ名", 160), ("enabled", "有効", 55),
            ("state", "状態", 120), ("last_file", "最終録画ファイル", 520), ("error", "最終エラー", 280),
        ]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.tag_configure("recording", foreground="#b00020")
        self.tree.tag_configure("working", foreground="#cc6d00")
        self.tree.tag_configure("warning", foreground="#9a6b00")
        self.tree.tag_configure("error", foreground="#b00020", background="#fff0f0")
        self.tree.tag_configure("stopped", foreground="#222222")
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

        actions = ttk.LabelFrame(self, text="操作ボタン（ボタン右側に動作説明）")
        actions.pack(fill="x", padx=10, pady=6)
        button_specs = [
            ("設定読込", self._reload_config_with_lamp, "保存済みの設定を読み直します"),
            ("設定保存", self._save_config_with_lamp, "カメラ設定と保存先設定を保存します"),
            ("接続テスト", self._test_connection_with_lamp, "選択中カメラのRTSP接続を確認します"),
            ("全録画開始", self._start_all_with_lamp, "有効な全カメラの録画を開始します"),
            ("全録画停止", self._stop_all_with_lamp, "全カメラの録画を停止します"),
            ("全カメラMP4区切り", self._split_all_with_lamp, "現在のMP4を確定して新しい録画に切替えます"),
            ("個別開始", self._start_selected_with_lamp, "選択中カメラだけ録画を開始します"),
            ("個別停止", self._stop_selected_with_lamp, "選択中カメラだけ録画を停止します"),
            ("容量整理", self._cleanup_with_lamp, "HDD空き容量を確認し古いMP4を整理します"),
        ]
        for index, (text, cmd, desc) in enumerate(button_specs):
            row = index % 3
            col_group = index // 3
            base_col = col_group * 2
            ttk.Button(actions, text=text, command=cmd, width=18).grid(row=row, column=base_col, sticky="ew", padx=(6, 4), pady=3)
            ttk.Label(actions, text=desc, style="Help.TLabel").grid(row=row, column=base_col + 1, sticky="w", padx=(0, 12), pady=3)
        for col in (1, 3, 5):
            actions.columnconfigure(col, weight=1)

        log_frame = ttk.LabelFrame(self, text="最新LOG（新しい順：一番上が最新）")
        log_frame.pack(fill="both", expand=True, padx=10, pady=8)
        self.log_text = tk.Text(log_frame, height=10)
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _build_drive_rows(self) -> None:
        for child in self.drives_frame.winfo_children():
            child.destroy()
        self.drive_widgets.clear()
        roots = unique_drive_roots(self.settings.get("system_dir", ""), self.settings.get("storage_dir", ""), self.settings.get("archive_dir", ""))
        if not roots:
            roots = [self.settings.get("archive_dir", "D:/NVR")]
        for row, root in enumerate(roots):
            ttk.Label(self.drives_frame, text=root, width=8).grid(row=row, column=0, sticky="w", padx=6, pady=2)
            bar = ttk.Progressbar(self.drives_frame, maximum=100, length=360)
            bar.grid(row=row, column=1, sticky="we", padx=6, pady=2)
            label = ttk.Label(self.drives_frame, text="確認中", width=52)
            label.grid(row=row, column=2, sticky="w", padx=6, pady=2)
            self.drive_widgets[root] = {"bar": bar, "label": label}
        self.drives_frame.columnconfigure(1, weight=1)

    def _show_operation(self, message: str, kind: str = "idle") -> None:
        colors = {
            "recording": "#b00020",
            "stopped": "#222222",
            "working": "#cc6d00",
            "success": "#006b3c",
            "warning": "#9a6b00",
            "error": "#b00020",
            "idle": "#333333",
        }
        self.operation_label.config(text=message, foreground=colors.get(kind, "#333333"))

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
        self._show_operation("● 保存先を変更中", "working")
        self._save_current_form()
        self.manager.stop_services()
        self.settings.update(build_dir_settings(system_dir, storage_dir))
        self.settings["min_free_gb"] = 5120
        self.store.save_settings(self.settings)
        self.store.save_cameras(self.cameras)
        self.logger = setup_logging(self.settings["logs_dir"])
        self._rebuild_manager()
        self._build_drive_rows()
        self._show_operation("● 保存先変更完了", "success")
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
        self._build_drive_rows()
        self.logger.info("設定読込")

    def _save_config_with_lamp(self) -> None:
        self._show_operation("● 設定保存中", "working")
        self._save_config()
        self._show_operation("● 設定保存完了", "success")

    def _reload_config_with_lamp(self) -> None:
        self._show_operation("● 設定読込中", "working")
        self._reload_config()
        self._show_operation("● 設定読込完了", "success")

    def _test_connection_with_lamp(self) -> None:
        self._show_operation("● 接続テスト中", "working")
        self._test_connection()
        self._show_operation("● 接続テスト完了", "success")

    def _start_all_with_lamp(self) -> None:
        self._show_operation("● 全カメラ録画開始", "recording")
        self.manager.start_all()

    def _stop_all_with_lamp(self) -> None:
        self._show_operation("■ 全カメラ録画停止", "stopped")
        self.manager.stop_all()

    def _start_selected_with_lamp(self) -> None:
        self._show_operation(f"● カメラ{self.selected_id.get()}録画開始", "recording")
        self.manager.start_camera(self.selected_id.get())

    def _stop_selected_with_lamp(self) -> None:
        self._show_operation(f"■ カメラ{self.selected_id.get()}録画停止", "stopped")
        self.manager.stop_camera(self.selected_id.get())

    def _split_all_with_lamp(self) -> None:
        self._show_operation("● 全カメラMP4区切り中", "working")
        self.manager.split_all("UI手動区切り")

    def _cleanup_with_lamp(self) -> None:
        self._show_operation("● 容量整理中", "working")
        self.manager.cleanup()
        self._show_operation("● 容量整理完了", "success")

    def _rebuild_manager(self) -> None:
        self.manager.stop_services()
        self.manager = RecorderManager(self.cameras, self.settings, self.logger)
        self.manager.start_services()

    def _test_connection(self) -> None:
        ok, msg = self.manager.test_camera(self.selected_id.get())
        if not ok:
            self._show_operation("× 接続テスト失敗", "error")
        messagebox.showinfo("接続テスト", ("成功: " if ok else "失敗: ") + msg)

    def _refresh_drive_status(self) -> None:
        for root, widgets in self.drive_widgets.items():
            bar = widgets["bar"]
            label = widgets["label"]
            try:
                usage = shutil.disk_usage(root)
                used_ratio = (usage.used / usage.total * 100) if usage.total else 0
                free_tb = usage.free / (1024 ** 4)
                total_tb = usage.total / (1024 ** 4)
                used_tb = usage.used / (1024 ** 4)
                bar.config(value=used_ratio)
                label.config(text=f"使用 {used_ratio:.1f}%  使用 {used_tb:.2f} TB / 空き {free_tb:.2f} TB / 全体 {total_tb:.2f} TB")
            except OSError as exc:
                bar.config(value=0)
                label.config(text=f"確認できません: {exc}")

    def _refresh_status(self) -> None:
        statuses = {s["id"]: s for s in self.manager.camera_statuses()}
        self.tree.delete(*self.tree.get_children())
        for cam in self.cameras:
            st = statuses.get(cam.id, {})
            state = st.get("recording_status", "")
            self.tree.insert("", "end", iid=str(cam.id), tags=(status_tag(state),), values=(
                cam.id, cam.name, "ON" if cam.enabled else "OFF", status_icon(state),
                st.get("last_completed_file", ""), st.get("last_error", ""),
            ))
        self._refresh_drive_status()
        self.path_label.config(text=f"一時: {self.settings['temp_dir']} / 完成MP4: {self.settings['archive_dir']}")
        log_path = Path(self.settings["logs_dir"]) / "app.log"
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("end", newest_first_log_text(text))
            self.log_text.yview_moveto(0.0)
        self.after(2000, self._refresh_status)

    def _on_close(self) -> None:
        self.manager.stop_services()
        self.destroy()


if __name__ == "__main__":
    LocalApp().mainloop()
