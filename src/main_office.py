"""Office PC read-only monitor UI plus command request creation."""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from commands.command_writer import CommandWriter
from status.status_reader import StatusReader


def mask_rtsp(url: str) -> str:
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:****@{host}"
    return url


class OfficeApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RTSP MP4録画システム（事務所PC）")
        self.geometry("1180x680")
        self.status_dir = tk.StringVar(value="D:/NVR/status")
        self.config_dir = tk.StringVar(value="D:/NVR/config")
        self.commands_dir = tk.StringVar(value="D:/NVR/commands")
        self.reader = StatusReader(self.status_dir.get(), self.config_dir.get())
        self.command_writer = CommandWriter(self.commands_dir.get())
        self._build_ui()
        self.after(1000, self._refresh)

    def _build_ui(self) -> None:
        paths = ttk.LabelFrame(self, text="共有フォルダパス（Archiveは閲覧のみ、状態/commandsは管理用）")
        paths.pack(fill="x", padx=10, pady=8)
        for row, (label, var, chooser) in enumerate([
            ("status", self.status_dir, self._choose_status),
            ("config", self.config_dir, self._choose_config),
            ("commands", self.commands_dir, self._choose_commands),
        ]):
            ttk.Label(paths, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=2)
            ttk.Entry(paths, textvariable=var, width=100).grid(row=row, column=1, sticky="we", padx=5, pady=2)
            ttk.Button(paths, text="参照", command=chooser).grid(row=row, column=2, padx=5)
        paths.columnconfigure(1, weight=1)

        actions = ttk.Frame(self)
        actions.pack(fill="x", padx=10, pady=4)
        ttk.Button(actions, text="更新", command=self._refresh).pack(side="left", padx=4)
        ttk.Button(actions, text="MP4区切り依頼", command=self._request_split).pack(side="left", padx=4)
        ttk.Button(actions, text="全録画開始依頼", command=lambda: self._write_command("start_all", {})).pack(side="left", padx=4)
        ttk.Button(actions, text="全録画停止依頼", command=lambda: self._write_command("stop_all", {})).pack(side="left", padx=4)

        self.system_label = ttk.Label(self, text="状態: -")
        self.system_label.pack(anchor="w", padx=10, pady=4)
        columns = ("id", "name", "enabled", "state", "start", "last_file", "error")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=18)
        for col, text, width in [
            ("id", "ID", 40), ("name", "カメラ名", 150), ("enabled", "有効", 55),
            ("state", "録画状態", 90), ("start", "現区間開始", 145),
            ("last_file", "最終録画ファイル", 500), ("error", "エラー", 260),
        ]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=8)
        config_frame = ttk.LabelFrame(self, text="設定内容（読み取り専用・RTSPパスワード伏せ字）")
        config_frame.pack(fill="x", padx=10, pady=4)
        self.config_text = tk.Text(config_frame, height=5)
        self.config_text.pack(fill="x", padx=4, pady=4)
        self.error_label = ttk.Label(self, text="", foreground="red")
        self.error_label.pack(anchor="w", padx=10)

    def _choose_status(self) -> None:
        self._choose_dir(self.status_dir)

    def _choose_config(self) -> None:
        self._choose_dir(self.config_dir)

    def _choose_commands(self) -> None:
        self._choose_dir(self.commands_dir)

    def _choose_dir(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(initialdir=var.get())
        if path:
            var.set(path)
            self._reset_io()

    def _reset_io(self) -> None:
        self.reader = StatusReader(self.status_dir.get(), self.config_dir.get())
        self.command_writer = CommandWriter(self.commands_dir.get())

    def _refresh(self) -> None:
        self._reset_io()
        status = self.reader.read_status()
        self.tree.delete(*self.tree.get_children())
        if status:
            system = status.get("system", {})
            self.system_label.config(text=(
                f"更新: {status.get('updated_at', '-')} / 状態: {system.get('status', '-')} / "
                f"空き容量: {system.get('disk_free_gb', '-')} GB / Archive: {system.get('archive_dir', '-')}"
            ))
            for cam in status.get("cameras", []):
                self.tree.insert("", "end", values=(
                    cam.get("id"), cam.get("name"), "ON" if cam.get("enabled") else "OFF",
                    cam.get("recording_status"), cam.get("current_segment_start"),
                    cam.get("last_completed_file"), cam.get("last_error"),
                ))
        else:
            self.system_label.config(text="状態JSONをまだ読めません")
        configs = self.reader.read_cameras_config()
        lines = []
        for cam in configs[:20]:
            lines.append(
                f"ID {cam.get('id')}: {cam.get('name', '')} / "
                f"enabled={cam.get('enabled')} / subdir={cam.get('save_subdir')} / "
                f"segment={cam.get('segment_minutes')}分 / retention={cam.get('retention_days')}日 / "
                f"rtsp={mask_rtsp(str(cam.get('rtsp_url', '')))}"
            )
        self.config_text.delete("1.0", "end")
        self.config_text.insert("end", "\n".join(lines))
        self.error_label.config(text=self.reader.last_error)
        self.after(3000, self._refresh)

    def _request_split(self) -> None:
        self._write_command("split_all_mp4", {"reason": "事務所PCからの手動区切り"})

    def _write_command(self, command_type: str, params: dict) -> None:
        try:
            self.command_writer = CommandWriter(self.commands_dir.get())
            path = self.command_writer.write_command(command_type, params)
            messagebox.showinfo("依頼作成", f"commands/pendingへ依頼を作成しました:\n{path}")
        except Exception as exc:
            messagebox.showerror("依頼作成失敗", str(exc))


if __name__ == "__main__":
    OfficeApp().mainloop()
