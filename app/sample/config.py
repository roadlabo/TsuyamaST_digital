
import base64
import json
import os
import sys
import subprocess
import shutil
import re
import webbrowser
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, filedialog, ttk

import cv2
import numpy as np

# ------------------------------------------------------------
# Path / launcher utilities
# ------------------------------------------------------------


def _resolve_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _resolve_base_dir()
MODULE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
HELP_PATH = BASE_DIR / "help.html"

print(f"[AICount] BASE_DIR={BASE_DIR}")
print(f"[AICount] ASSETS_DIR={ASSETS_DIR} exist={ASSETS_DIR.exists()}")
print(f"[AICount] HELP_PATH={HELP_PATH} exist={HELP_PATH.exists()}")

dist11_exe = BASE_DIR / "dist" / "AICount11.exe"
dist_exe = BASE_DIR / "dist" / "AICount.exe"
installed11_exe = Path(r"C:\Program Files\RoadLabo\AICount\AICount11.exe")
installed_exe = Path(r"C:\Program Files\RoadLabo\AICount\AICount.exe")
py_exe = shutil.which("python") or sys.executable
aicount11_py = BASE_DIR / "src" / "AICount11.py"
aicount_py = BASE_DIR / "src" / "AICount.py"


def _find_aicount_exe() -> str | None:
    # 1) UIと同じフォルダ
    here = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    cand = os.path.join(here, "AICount.exe")
    if os.path.exists(cand):
        return cand
    # 2) 既定のインストール先
    cand = r"C:\\Program Files\\RoadLabo\\AICount\\AICount.exe"
    if os.path.exists(cand):
        return cand
    return None


def _resolve_aicount_command() -> list[str]:
    """
    AICount 実行ファイルを探索して最適な実行方法を返す
    - 開発中（非frozen環境）では src/AICount11.py → src/AICount.py の順で最優先
    - ビルド済み(dist配下)は AICount11.exe → AICount.exe
    - インストール済みは  AICount11.exe → AICount.exe
    """

    # ① 開発中 .py を最優先（v11 → 旧）
    if not getattr(sys, "frozen", False):
        if aicount11_py.exists():
            print(f"[AICount] Using source: {aicount11_py}")
            return [py_exe, "-u", str(aicount11_py)]
        if aicount_py.exists():
            print(f"[AICount] Using source (fallback): {aicount_py}")
            return [py_exe, "-u", str(aicount_py)]

    # ② dist の v11 → 旧
    if dist11_exe.exists():
        print(f"[AICount] Using local EXE: {dist11_exe}")
        return [str(dist11_exe)]
    if dist_exe.exists():
        print(f"[AICount] Using local EXE (fallback): {dist_exe}")
        return [str(dist_exe)]

    # ③ インストール済み v11 → 旧
    if installed11_exe.exists():
        print(f"[AICount] Using installed EXE: {installed11_exe}")
        return [str(installed11_exe)]
    if installed_exe.exists():
        print(f"[AICount] Using installed EXE (fallback): {installed_exe}")
        return [str(installed_exe)]

    raise FileNotFoundError("AICount 実行ファイルが見つかりません。")

CONFIG_TITLE_PATH = ASSETS_DIR / "config_title.png"
ROADLABO_ICON_PATH = ASSETS_DIR / "roadlabo_icon.png"
APP_ICON_PATH = ASSETS_DIR / "icon.ico"

DEFAULT_VIDEO_DIR = ""
DEFAULT_OUTPUT_DIR = ""
MAX_WIDTH = 735
MAX_HEIGHT = 956


def _create_fullscreen_window(name: str):
    """Create a resizable OpenCV window and switch it to fullscreen if possible."""
    cv2.namedWindow(name, cv2.WINDOW_NORMAL)
    try:
        cv2.setWindowProperty(name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    except cv2.error:
        # Some environments may not support fullscreen property; ignore silently.
        pass


def _warn_if_ffmpeg_disabled(master=None):
    """OpenCVにFFmpegが無効な場合、MTSが読めない可能性を警告。"""
    try:
        info = cv2.getBuildInformation()
    except Exception:
        return
    if "FFMPEG:                      YES" not in info:
        try:
            messagebox.showwarning(
                "FFmpeg 無効の可能性",
                "OpenCVにFFmpegが有効化されていない可能性があります。\n"
                "MTS(AVCHD)の読み込みに失敗する場合は、'opencv-python' を使用してください。\n"
                "（'opencv-python-headless' では読めないことがあります）",
                parent=master
            )
        except Exception:
            pass


def _enforce_dialog_constraints(win: tk.Toplevel):
    """Keep dialog windows on top and prevent closing/minimising via the frame controls."""

    state = {"closing": False}

    def mark_closing():
        state["closing"] = True

    win.protocol("WM_DELETE_WINDOW", lambda: None)

    try:
        win.attributes("-topmost", True)
    except tk.TclError:
        pass
    win.lift()
    win.focus_force()

    def handle_unmap(event):  # noqa: ARG001 - required callback signature
        if not state["closing"]:
            win.after(0, win.deiconify)
            win.after(0, win.lift)

    win.bind("<Unmap>", handle_unmap, add="+")
    win.bind("<FocusOut>", lambda event: win.lift(), add="+")

    return mark_closing


def _load_resized_photo_image(path, height: int, master=None):
    """Load an image file and return a PhotoImage resized to the specified height."""

    path = os.fspath(path)
    if not os.path.exists(path):
        return None, f"{os.path.basename(path)} が見つかりません"

    image = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if image is None or image.size == 0:
        return None, f"{os.path.basename(path)} の読み込みに失敗しました"

    orig_height, orig_width = image.shape[:2]
    if orig_height <= 0:
        return None, f"{os.path.basename(path)} の高さが不正です"

    new_height = max(1, int(height))
    new_width = max(1, int(round(orig_width * (new_height / orig_height))))
    interpolation = cv2.INTER_AREA if new_height < orig_height else cv2.INTER_LINEAR
    resized = cv2.resize(image, (new_width, new_height), interpolation=interpolation)

    success, buffer = cv2.imencode(".png", resized)
    if not success:
        return None, f"{os.path.basename(path)} のエンコードに失敗しました"

    encoded = base64.b64encode(buffer).decode("ascii")
    photo = tk.PhotoImage(master=master, data=encoded, format="png")
    return photo, None


def open_help(master=None):
    if HELP_PATH.exists():
        try:
            opened = webbrowser.open(HELP_PATH.as_uri())
        except Exception:
            try:
                opened = webbrowser.open(str(HELP_PATH))
            except Exception as exc:
                messagebox.showwarning(
                    "ヘルプ",
                    f"help.html を開けませんでした:\n{exc}",
                    parent=master,
                )
                return False
        if not opened:
            messagebox.showwarning(
                "ヘルプ",
                "help.html を開けませんでした。",
                parent=master,
            )
            return False
        return True

    messagebox.showwarning(
        "ヘルプ",
        f"help.html が見つかりません:\n{HELP_PATH}",
        parent=master,
    )
    return False


def launch_aicount_with_args(args: list[str], *, master=None):
    try:
        cmd = _resolve_aicount_command()
    except FileNotFoundError as exc:
        messagebox.showerror(
            "エラー",
            str(exc),
            parent=master,
        )
        return None

    cmd = cmd + list(args)
    print(f"[AICount] Launch: {cmd}")
    try:
        return subprocess.Popen(cmd)
    except Exception as exc:
        messagebox.showerror(
            "起動失敗",
            f"AICount の起動に失敗しました。\n{exc}",
            parent=master,
        )
        return None


def _segments_intersect(p1, p2, p3, p4) -> bool:
    """Return True if line segment p1-p2 intersects p3-p4 (including touching)."""

    def orientation(a, b, c):
        val = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if val > 0:
            return 1
        if val < 0:
            return -1
        return 0

    def on_segment(a, b, c):
        return (
            min(a[0], c[0]) <= b[0] <= max(a[0], c[0])
            and min(a[1], c[1]) <= b[1] <= max(a[1], c[1])
        )

    o1 = orientation(p1, p2, p3)
    o2 = orientation(p1, p2, p4)
    o3 = orientation(p3, p4, p1)
    o4 = orientation(p3, p4, p2)

    if o1 != o2 and o3 != o4:
        return True

    # Collinear cases
    if o1 == 0 and on_segment(p1, p3, p2):
        return True
    if o2 == 0 and on_segment(p1, p4, p2):
        return True
    if o3 == 0 and on_segment(p3, p1, p4):
        return True
    if o4 == 0 and on_segment(p3, p2, p4):
        return True
    return False


def _polygon_self_intersects(points) -> bool:
    """Check if polygon defined by points has self-intersections."""
    n = len(points)
    if n < 4:  # triangles cannot self-intersect
        return False
    edges = [
        (points[i], points[(i + 1) % n])
        for i in range(n)
    ]
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            # Skip adjacent edges sharing a vertex
            if j == i:
                continue
            if (j == (i + 1) % n) or (i == (j + 1) % n):
                continue
            if _segments_intersect(edges[i][0], edges[i][1], edges[j][0], edges[j][1]):
                return True
    return False

# ----------------- Defaults (line_color & fourcc removed) -----------------
DEFAULTS = {
    "crossing_system": 1,
    "line_start": [452, 500],
    "line_end": [4, 450],
    "p1": [743, 110],
    "p2": [1096, 233],
    "p3": [2, 388],
    "p4": [42, 132],
    "exclude_polygon": [[197, 0], [179, 288], [243, 390], [463, 515], [803, 632], [1029, 687], [1276, 686], [1277, 0]],
    "crossing_judgment_pattern": 2,
    "tracking_method": "bytetrack",
    "yolo_model": "yolo11l",
    # Keep the derived size in sync so the analyzer CLI can honour the UI choice
    "yolo11_size": "l",
    "target_classes": [2,3,5,7],
    "confidence_threshold": 0.05,
    "distance_threshold": 200,
    "iou_threshold": 0.5,
    "frame_skip": 3,
    "frame_rate_original": 5.0,
    "std_acc": 1.0,
    "x_std_meas": 1.0,
    "y_std_meas": 1.0,
    "max_iou_distance": 0.7,
    "max_age": 30,
    "n_init": 3,
    "nn_budget": 100,
    "timestamp_ocr": True,
    "ocr_box": [906, 689, 1275, 719],
    "show_estimated_time": False,
    "start_time_str": "2025-01-01 08:53:41",
    "end_time_str": "2025-01-01 18:05:32",
    "preview": True,
    "video_dir": DEFAULT_VIDEO_DIR,
    "output_dir": DEFAULT_OUTPUT_DIR,
    "enable_congestion": True,
    "congestion_calculation_interval": 10,
    "search_subdirs": False,
    "bt_track_high_thresh": 0.3,
    "bt_track_low_thresh": 0.01,
    "bt_new_track_thresh": 0.5,
    "bt_match_thresh": 0.9,
    "bt_track_buffer": 200,
}

MODELS = ["yolo11n", "yolo11s", "yolo11m", "yolo11l", "yolo11x"]


def _migrate_legacy_values(values: dict) -> dict:
    v = dict(values)
    v5_to_v11 = {
        "yolov5s": "yolo11n",
        "yolov5m": "yolo11s",
        "yolov5l": "yolo11m",
        "yolov5x": "yolo11l",
    }
    model = v.get("yolo_model")
    if model in v5_to_v11:
        v["yolo_model"] = v5_to_v11[model]

    model = v.get("yolo_model", "")
    if isinstance(model, str) and model.startswith("yolo11") and len(model) >= 7:
        v["yolo11_size"] = model[6:]
    else:
        v.setdefault("yolo11_size", DEFAULTS.get("yolo11_size", "m"))

    for key in (
        "bt_track_high_thresh",
        "bt_track_low_thresh",
        "bt_new_track_thresh",
        "bt_match_thresh",
        "bt_track_buffer",
    ):
        v.setdefault(key, DEFAULTS.get(key))

    for key in (
        "max_iou_distance",
        "max_age",
        "n_init",
        "nn_budget",
    ):
        v.setdefault(key, DEFAULTS.get(key))

    v.setdefault("enable_congestion", True)

    tm = (v.get("tracking_method") or "").lower()
    alias = {
        "deep sort": "deepsort",
        "byte": "bytetrack",
        "byte-track": "bytetrack",
        "byte track": "bytetrack",
    }
    if tm in alias:
        v["tracking_method"] = alias[tm]

    if "video_dir" not in v and "video_folder" in v:
        v["video_dir"] = v.get("video_folder", "")

    v["tracking_method"] = "bytetrack"

    return v

# ----------------- Small helpers -----------------
def str_to_point_list(s):
    pts = []
    s = s.strip()
    if not s:
        return pts
    for pair in s.split(';'):
        if not pair.strip():
            continue
        xy = pair.split(',')
        if len(xy) != 2:
            raise ValueError("座標は 'x,y; x,y' 形式で入力してください")
        pts.append([int(xy[0].strip()), int(xy[1].strip())])
    return pts

def point_list_to_str(pts):
    return '; '.join(f"{x},{y}" for x,y in pts)

def _float_or_none(s: str):
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int_or_none(s: str):
    s = s.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def confirm_ok_retry(title="確認", message="OKで確定 / もう一度でやり直し"):
    """Custom modal dialog: returns True if OK, False if Retry."""
    win = tk.Toplevel()
    win.title(title)
    win.resizable(False, False)
    win.grab_set()
    mark_closing = _enforce_dialog_constraints(win)
    lbl = tk.Label(win, text=message, padx=16, pady=12)
    lbl.pack()
    result = {"ok": None}
    btns = tk.Frame(win)
    btns.pack(padx=12, pady=8)
    def do_ok():
        result["ok"] = True
        mark_closing()
        win.destroy()
    def do_retry():
        result["ok"] = False
        mark_closing()
        win.destroy()
    tk.Button(btns, text="OK", width=10, command=do_ok).pack(side="left", padx=6)
    tk.Button(btns, text="もう一度", width=10, command=do_retry).pack(side="left", padx=6)
    win.wait_window()
    return bool(result["ok"])

# ----------------- Main UI -----------------


class ConfigUI(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            self.iconbitmap(os.fspath(APP_ICON_PATH))
        except Exception as exc:
            print(f"[AICount] iconbitmap load failed: {exc}")
        self.title("AIカウント設定エディタ (v11)")
        self.resizable(True, True)

        self.values = _migrate_legacy_values(DEFAULTS.copy())

        self.tracking_method_var = tk.StringVar(value="bytetrack")
        self.target_classes_var = tk.StringVar()
        self.confidence_var = tk.StringVar(value="0.05")
        self.distance_threshold_var = tk.StringVar(value="200")
        self.iou_threshold_var = tk.StringVar(value="0.5")
        self.frame_skip_var = tk.StringVar(value="3")
        self.frame_rate_original_var = tk.StringVar(value="5.0")
        self.std_acc_var = tk.StringVar(value="1.0")
        self.x_std_meas_var = tk.StringVar(value="1.0")
        self.y_std_meas_var = tk.StringVar(value="1.0")
        self.max_iou_distance_var = tk.StringVar(value=str(DEFAULTS.get("max_iou_distance", 0.7)))
        self.max_age_var = tk.StringVar(value=str(DEFAULTS.get("max_age", 30)))
        self.n_init_var = tk.StringVar(value=str(DEFAULTS.get("n_init", 3)))
        self.nn_budget_var = tk.StringVar(value=str(DEFAULTS.get("nn_budget", 100)))
        self.congestion_interval_var = tk.StringVar(value="10")
        self.enable_congestion_var = tk.BooleanVar(value=True)

        self.bt_high_var = tk.StringVar(value=str(DEFAULTS.get("bt_track_high_thresh", 0.6)))
        self.bt_low_var = tk.StringVar(value=str(DEFAULTS.get("bt_track_low_thresh", 0.1)))
        self.bt_match_var = tk.StringVar(value=str(DEFAULTS.get("bt_match_thresh", 0.6)))
        self.bt_buffer_var = tk.StringVar(value=str(DEFAULTS.get("bt_track_buffer", 20)))

        self.create_widgets()
        self.populate_fields()
        self.update_tracker_param_state()
        self.after(0, self._fit_window_width)

        # 起動時に一度だけFFmpeg有効性を警告（必要時のみ）
        _warn_if_ffmpeg_disabled(master=self)

    def _process_events(self):
        """Process pending Tk events safely while OpenCV loop is running."""

        try:
            self.update_idletasks()
            self.update()
        except tk.TclError:
            # The window might be closed while processing events; ignore.
            pass

    def create_widgets(self):
        main_area = tk.Frame(self)
        main_area.pack(side="top", fill="both", expand=True)
        self.main_area = main_area

        canvas = tk.Canvas(main_area, borderwidth=0)
        frame = tk.Frame(canvas)
        self.scroll_canvas = canvas
        self.scrollable_frame = frame
        vsb = tk.Scrollbar(main_area, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)

        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.create_window((0, 0), window=frame, anchor="nw")

        def on_frame_config(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        frame.bind("<Configure>", on_frame_config)

        def on_mousewheel(event):  # noqa: ARG001 - Tkinter callback signature
            if canvas.winfo_containing(event.x_root, event.y_root) is None:
                return
            if event.delta:
                canvas.yview_scroll(int(-event.delta / 120), "units")
            else:
                num = getattr(event, "num", None)
                if num == 4:
                    canvas.yview_scroll(-1, "units")
                elif num == 5:
                    canvas.yview_scroll(1, "units")

        canvas.bind_all("<MouseWheel>", on_mousewheel)
        canvas.bind_all("<Button-4>", on_mousewheel)
        canvas.bind_all("<Button-5>", on_mousewheel)
        frame.columnconfigure(0, weight=1)

        row = 0

        title_path = CONFIG_TITLE_PATH
        self.header_image = None
        header_error = None
        self.header_image, header_error = _load_resized_photo_image(title_path, 50, master=self)

        header_frame = ttk.Frame(frame)
        header_frame.grid(row=row, column=0, pady=(12, 8), sticky="we")

        if self.header_image is not None:
            tk.Label(header_frame, image=self.header_image).pack(side="left", padx=(0, 8))
        else:
            tk.Label(
                header_frame,
                text=header_error or "config_title.png が見つかりません",
                font=("TkDefaultFont", 14, "bold"),
                pady=12,
            ).pack(side="left", padx=(0, 8))
        self.help_button = ttk.Button(
            header_frame,
            text="HELP",
            command=lambda: open_help(master=self),
        )
        self.help_button.pack(side="left", padx=(0, 8))
        row += 1

        # --- Section: Region setup ---
        region_frame = ttk.LabelFrame(frame, text="領域設定")
        region_frame.grid(row=row, column=0, sticky="we", padx=12, pady=8)
        region_frame.columnconfigure(1, weight=1)
        row += 1

        ttk.Label(region_frame, text="交差判定方式 (1:1本線, 2:4本線)").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.crossing_system = ttk.Combobox(region_frame, values=[1, 2], width=10, state="readonly")
        self.crossing_system.grid(row=0, column=1, sticky="w", pady=(0, 4))
        self.crossing_system.bind("<<ComboboxSelected>>", self._on_crossing_system_change)

        self.line_coords_var = tk.StringVar()
        self.line_pick_button = tk.Button(
            region_frame,
            text="座標入力（line_start/line_end）",
            command=self.pick_line_start_end,
        )
        self.line_pick_button.grid(row=1, column=0, sticky="w", pady=(4, 2))
        ttk.Label(region_frame, textvariable=self.line_coords_var).grid(row=1, column=1, sticky="w", padx=(8, 0))

        self.p1234_coords_var = tk.StringVar()
        self.p1234_pick_button = tk.Button(
            region_frame,
            text="座標入力（p1→p2→p3→p4）",
            command=self.pick_p1_p4,
        )
        self.p1234_pick_button.grid(row=3, column=0, sticky="w", pady=(8, 2))
        ttk.Label(region_frame, textvariable=self.p1234_coords_var).grid(row=3, column=1, sticky="w", padx=(8, 0))

        self.exclude_polygon_var = tk.StringVar()
        tk.Button(region_frame, text="座標入力（除外ポリゴン）", command=self.pick_exclude_polygon).grid(row=5, column=0, sticky="w", pady=(8, 2))
        ttk.Label(region_frame, textvariable=self.exclude_polygon_var, wraplength=420, justify="left").grid(row=5, column=1, sticky="w", padx=(8, 0))

        for c in range(2):
            region_frame.grid_columnconfigure(c, weight=1 if c == 1 else 0)

        # --- Section: Detection settings ---
        detect_frame = ttk.LabelFrame(frame, text="検出設定")
        detect_frame.grid(row=row, column=0, sticky="we", padx=12, pady=8)
        for col in (1, 3):
            detect_frame.columnconfigure(col, weight=1)
        row += 1

        ttk.Label(
            detect_frame,
            text="判定ロジック (1:YOLO再発見時のみ, 2:予測時も判定)",
        ).grid(row=0, column=0, sticky="w")
        self.crossing_judgment_pattern = ttk.Combobox(
            detect_frame, values=[1, 2], width=10, state="readonly"
        )
        self.crossing_judgment_pattern.grid(row=0, column=1, sticky="we", pady=(0, 4))

        ttk.Label(detect_frame, text="YOLOv11 モデル").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.yolo_model = ttk.Combobox(
            detect_frame, values=MODELS, width=12, state="readonly"
        )
        self.yolo_model.grid(row=0, column=3, sticky="we", pady=(0, 4))

        ttk.Label(detect_frame, text="対象クラス (カンマ区切り)").grid(row=1, column=0, sticky="w")
        self.target_classes = ttk.Entry(detect_frame, width=30, textvariable=self.target_classes_var)
        self.target_classes.grid(row=1, column=1, columnspan=3, sticky="we")

        ttk.Label(
            detect_frame,
            text=" 0 - 人, 1 - 自転車, 2 - 車, 3 - オートバイ, 4 - 飛行機, 5 - バス, 6 - 電車, 7 - トラック",
            wraplength=500,
            justify="left",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 8))

        param_row = 3

        # confidence_threshold (全トラッカー共通で使用)
        ttk.Label(detect_frame, text="confidence_threshold").grid(
            row=param_row, column=0, sticky="w"
        )
        self.conf_entry = ttk.Entry(
            detect_frame, textvariable=self.confidence_var, width=10
        )
        self.conf_entry.grid(row=param_row, column=1, sticky="w")

        # 右側に簡単な説明ラベルを追加（具体的な数値例は help.html に記載）
        ttk.Label(
            detect_frame,
            text="YOLO検出の信頼度しきい値（低いほど検出は増えるが誤検出も増加）",
        ).grid(row=param_row, column=2, columnspan=2, sticky="w")

        param_row += 1

        # frame_skip
        ttk.Label(detect_frame, text="frame_skip").grid(row=param_row, column=0, sticky="w")
        self.frame_skip_entry = ttk.Entry(
            detect_frame, textvariable=self.frame_skip_var, width=10
        )
        self.frame_skip_entry.grid(row=param_row, column=1, sticky="w")

        param_row += 1

        # frame_rate_original
        ttk.Label(detect_frame, text="frame_rate_original").grid(row=param_row, column=0, sticky="w")
        self.frame_rate_original_entry = ttk.Entry(
            detect_frame, textvariable=self.frame_rate_original_var, width=10
        )

        # ラベルの右隣に Entry をきちんと配置する
        self.frame_rate_original_entry.grid(row=param_row, column=1, sticky="w")

        param_row += 1

        # congestion_calculation_interval
        tk.Label(detect_frame, text="congestion_calculation_interval").grid(
            row=param_row, column=0, sticky="w"
        )
        self.congestion_interval_entry = tk.Entry(
            detect_frame, textvariable=self.congestion_interval_var, width=10
        )
        self.congestion_interval_entry.grid(row=param_row, column=1, sticky="w")

        tk.Checkbutton(
            detect_frame,
            text="渋滞指標算出（OCR時はOFFにしてください）",
            variable=self.enable_congestion_var,
        ).grid(row=param_row, column=2, sticky="w")

        param_row += 1

        self.dh_widgets = []
        self.bt_frame = ttk.LabelFrame(detect_frame, text="ByteTrack 専用パラメータ")
        self.bt_frame.grid(row=param_row, column=0, columnspan=4, sticky="we", pady=(8, 4))

        bt_row_frame = ttk.Frame(self.bt_frame)
        bt_row_frame.grid(row=0, column=0, sticky="w", padx=5, pady=5)

        bt_items = [
            # ByteTrack.__init__ の引数名をコメントで明示
            # 具体的な数値例は help.html 側に集約し、ここでは役割だけを簡潔に表示する
            ("track_thresh", self.bt_high_var,
             "通常追跡に使うスコアしきい値"),
            ("min_conf", self.bt_low_var,
             "低信頼度も含めて拾う下限スコア"),
            ("match_thresh", self.bt_match_var,
             "IoU によるマッチングのしきい値"),
            ("track_buffer", self.bt_buffer_var,
             "見失ってから保持するフレーム数"),
        ]

        self.bt_widgets = []
        for row, (label_text, var, hint) in enumerate(bt_items):
            ttk.Label(bt_row_frame, text=label_text).grid(row=row, column=0, sticky="w")
            entry = ttk.Entry(bt_row_frame, textvariable=var, width=10)
            entry.grid(row=row, column=1, sticky="w")
            ttk.Label(bt_row_frame, text=hint).grid(row=row, column=2, sticky="w")

            if label_text == "track_thresh":
                self.bt_high_entry = entry
            elif label_text == "min_conf":
                self.bt_low_entry = entry
            elif label_text == "match_thresh":
                self.bt_match_entry = entry
            elif label_text == "track_buffer":
                self.bt_buffer_entry = entry

            self.bt_widgets.extend([entry])

        # --- Section: 時間管理 ---
        misc_frame = ttk.LabelFrame(frame, text="時間管理")
        misc_frame.grid(row=row, column=0, sticky="we", padx=12, pady=8)
        misc_frame.columnconfigure(0, weight=0)
        misc_frame.columnconfigure(1, weight=0)
        misc_frame.columnconfigure(2, weight=1)
        row += 1

        self.timestamp_ocr_var = tk.BooleanVar()
        self.show_estimated_time_var = tk.BooleanVar()
        self.preview_var = tk.BooleanVar(value=True)
        self.search_subdirs_var = tk.BooleanVar()

        ttk.Checkbutton(
            misc_frame,
            text="timestamp_ocr",
            variable=self.timestamp_ocr_var,
            command=self._on_timestamp_toggle,
        ).grid(row=0, column=0, sticky="w", pady=(0, 2))

        self.ocr_box_var = tk.StringVar()
        self.ocr_box_button = tk.Button(
            misc_frame,
            text="座標入力（OCRボックス 左上→右下）",
            command=self.pick_ocr_box,
        )
        self.ocr_box_button.grid(row=0, column=1, sticky="w", padx=(12, 0), pady=(0, 2))
        ttk.Label(misc_frame, textvariable=self.ocr_box_var).grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Checkbutton(
            misc_frame,
            text="show_estimated_time",
            variable=self.show_estimated_time_var,
            command=self._on_show_estimated_toggle,
        ).grid(row=1, column=0, sticky="w", pady=(6, 2))

        ttk.Label(
            misc_frame,
            text="start_time_str (YYYY-MM-DD HH:MM:SS または HH:MM:SS)"
        ).grid(row=2, column=0, sticky="w")
        self.start_time_str = ttk.Entry(misc_frame, width=20)
        self.start_time_str.grid(row=2, column=1, sticky="w")

        ttk.Label(
            misc_frame,
            text="end_time_str (YYYY-MM-DD HH:MM:SS または HH:MM:SS)"
        ).grid(row=3, column=0, sticky="w")
        self.end_time_str = ttk.Entry(misc_frame, width=20)
        self.end_time_str.grid(row=3, column=1, sticky="w")

        # --- Section: 入出力 ---
        io_frame = ttk.LabelFrame(frame, text="入出力")
        io_frame.grid(row=row, column=0, sticky="we", padx=12, pady=8)
        io_frame.columnconfigure(1, weight=1)
        row += 1

        time_validate = (self.register(self._validate_time_entry), "%P", "%W")
        time_invalid = (self.register(self._handle_invalid_time), "%W")
        self.start_time_str.config(validate="key", validatecommand=time_validate, invalidcommand=time_invalid)
        self.end_time_str.config(validate="key", validatecommand=time_validate, invalidcommand=time_invalid)

        target_validate = (self.register(self._validate_target_classes_entry), "%P", "%W")
        target_invalid = (self.register(self._handle_invalid_target_classes), "%W")
        self.target_classes.config(validate="key", validatecommand=target_validate, invalidcommand=target_invalid)

        ttk.Label(io_frame, text="video_dir").grid(row=0, column=0, sticky="w")
        self.video_dir = ttk.Entry(io_frame, width=40)
        self.video_dir.grid(row=0, column=1, sticky="we")
        ttk.Button(io_frame, text="参照…", command=self.browse_video_dir).grid(row=0, column=2, padx=4)

        ttk.Checkbutton(
            io_frame,
            text="サブフォルダも検索する",
            variable=self.search_subdirs_var,
        ).grid(row=1, column=1, sticky="w", pady=(6, 0))

        ttk.Label(io_frame, text="output_dir").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.output_dir = ttk.Entry(io_frame, width=40)
        self.output_dir.grid(row=2, column=1, sticky="we", pady=(6, 0))
        ttk.Button(io_frame, text="参照…", command=self.browse_output_dir).grid(row=2, column=2, padx=4, pady=(6, 0))

        # --- Section: Actions ---
        btns = tk.Frame(frame)
        btns.grid(row=row, column=0, pady=12, padx=12, sticky="w")
        ttk.Button(btns, text="新規(デフォルト読込)", command=self.reset_defaults).pack(side="left", padx=4)
        ttk.Button(btns, text="設定ファイルを開く…", command=self.load_config).pack(side="left", padx=4)
        ttk.Button(btns, text="設定を保存して解析実行", command=self.save_config).pack(side="left", padx=4)
        icon_path = ROADLABO_ICON_PATH
        self.roadlabo_icon = None
        self.roadlabo_icon, icon_error = _load_resized_photo_image(icon_path, 30, master=self)

        # --- RoadLabo link setup ---
        def open_roadlabo(event=None):
            webbrowser.open("https://roadlabo.com/")

        if self.roadlabo_icon is not None:
            icon_label = tk.Label(btns, image=self.roadlabo_icon, cursor="hand2")
            icon_label.pack(side="left", padx=(4, 0))
            icon_label.bind("<Button-1>", open_roadlabo)
        else:
            if icon_error:
                tk.Label(btns, text=icon_error, foreground="red").pack(side="left", padx=(8, 0))

        # URLラベルを追加（クリックでも開く）
        url_label = tk.Label(
            btns,
            text="https://roadlabo.com/",
            fg="blue",
            cursor="hand2",
            font=("TkDefaultFont", 9, "underline")
        )
        url_label.pack(side="left", padx=(4, 0))
        url_label.bind("<Button-1>", open_roadlabo)

    # -------------- Populate --------------
    def populate_fields(self):
        self._make_time_entries_editable()
        self.values = _migrate_legacy_values(self.values)
        v = self.values
        self.crossing_system.set(v["crossing_system"])
        self._update_crossing_buttons()

        # reflect to readonly entries
        self.line_coords_var.set(
            " / ".join(
                [
                    f"line_start=({v['line_start'][0]},{v['line_start'][1]})",
                    f"line_end=({v['line_end'][0]},{v['line_end'][1]})",
                ]
            )
        )
        self.p1234_coords_var.set(
            " / ".join(
                [
                    f"p1=({v['p1'][0]},{v['p1'][1]})",
                    f"p2=({v['p2'][0]},{v['p2'][1]})",
                    f"p3=({v['p3'][0]},{v['p3'][1]})",
                    f"p4=({v['p4'][0]},{v['p4'][1]})",
                ]
            )
        )
        self.exclude_polygon_var.set(
            "exclude_polygon=" + point_list_to_str(v["exclude_polygon"])
        )
        self.ocr_box_var.set(
            "ocr_box=" + ", ".join(str(x) for x in v["ocr_box"])
        )

        self.crossing_judgment_pattern.set(v["crossing_judgment_pattern"])
        self.tracking_method_var.set("bytetrack")
        self.yolo_model.set(v["yolo_model"])
        self.target_classes_var.set(", ".join(str(x) for x in v["target_classes"]))

        self.confidence_var.set(str(v["confidence_threshold"]))
        self.distance_threshold_var.set(str(v["distance_threshold"]))
        self.iou_threshold_var.set(str(v["iou_threshold"]))
        self.frame_skip_var.set(str(v["frame_skip"]))
        self.frame_rate_original_var.set(str(v["frame_rate_original"]))
        self.std_acc_var.set(str(v["std_acc"]))
        self.x_std_meas_var.set(str(v["x_std_meas"]))
        self.y_std_meas_var.set(str(v["y_std_meas"]))
        self.max_iou_distance_var.set(str(v["max_iou_distance"]))
        self.max_age_var.set(str(v["max_age"]))
        self.n_init_var.set(str(v["n_init"]))
        self.nn_budget_var.set(str(v["nn_budget"]))
        self.congestion_interval_var.set(str(v["congestion_calculation_interval"]))
        self.enable_congestion_var.set(v.get("enable_congestion", True))

        bt_high = v.get("bt_track_high_thresh")
        bt_low = v.get("bt_track_low_thresh")
        bt_match = v.get("bt_match_thresh")
        bt_buffer = v.get("bt_track_buffer")

        if bt_high is None:
            bt_high = DEFAULTS.get("bt_track_high_thresh", "")
        if bt_low is None:
            bt_low = DEFAULTS.get("bt_track_low_thresh", "")
        if bt_match is None:
            bt_match = DEFAULTS.get("bt_match_thresh", "")
        if bt_buffer is None:
            bt_buffer = DEFAULTS.get("bt_track_buffer", "")

        self.bt_high_var.set("" if bt_high == "" else str(bt_high))
        self.bt_low_var.set("" if bt_low == "" else str(bt_low))
        self.bt_match_var.set("" if bt_match == "" else str(bt_match))
        self.bt_buffer_var.set("" if bt_buffer == "" else str(bt_buffer))

        self.timestamp_ocr_var.set(v["timestamp_ocr"])
        self.show_estimated_time_var.set(v["show_estimated_time"])
        if self.timestamp_ocr_var.get() and self.show_estimated_time_var.get():
            self.show_estimated_time_var.set(False)
        self.preview_var.set(True)
        self.search_subdirs_var.set(v.get("search_subdirs", DEFAULTS["search_subdirs"]))

        self.start_time_str.delete(0, tk.END)
        self.start_time_str.insert(0, v["start_time_str"])
        self.end_time_str.delete(0, tk.END)
        self.end_time_str.insert(0, v["end_time_str"])
        self.video_dir.delete(0, tk.END)
        self.video_dir.insert(0, v.get("video_dir", DEFAULT_VIDEO_DIR))
        self.output_dir.delete(0, tk.END)
        self.output_dir.insert(0, v["output_dir"])

        self._update_time_controls()
        self.update_tracker_param_state()

    def _fit_window_width(self):
        """Set the default window size and maximum size to 735×956."""

        try:
            # デフォルトのウィンドウサイズを固定
            self.geometry(f"{MAX_WIDTH}x{MAX_HEIGHT}")

            # これより大きくできないようにする
            self.maxsize(MAX_WIDTH, MAX_HEIGHT)

        except tk.TclError:
            pass

    def _on_crossing_system_change(self, event=None):  # noqa: ARG002 - required by bind
        self._update_crossing_buttons()

    def _update_crossing_buttons(self):
        try:
            value = int(self.crossing_system.get())
        except (TypeError, ValueError):
            value = None

        if value == 1:
            self.line_pick_button.config(state=tk.NORMAL)
            self.p1234_pick_button.config(state=tk.DISABLED)
        elif value == 2:
            self.line_pick_button.config(state=tk.DISABLED)
            self.p1234_pick_button.config(state=tk.NORMAL)
        else:
            self.line_pick_button.config(state=tk.NORMAL)
            self.p1234_pick_button.config(state=tk.NORMAL)

    def _on_timestamp_toggle(self):
        if self.timestamp_ocr_var.get():
            self.show_estimated_time_var.set(False)
        self._update_time_controls()

    def _on_show_estimated_toggle(self):
        if self.show_estimated_time_var.get():
            self.timestamp_ocr_var.set(False)
        self._update_time_controls()

    def _update_time_controls(self):
        timestamp_enabled = bool(self.timestamp_ocr_var.get())
        estimated_enabled = bool(self.show_estimated_time_var.get())

        self.start_time_str.config(state=tk.DISABLED if timestamp_enabled else tk.NORMAL)
        self.end_time_str.config(state=tk.DISABLED if timestamp_enabled else tk.NORMAL)
        self.ocr_box_button.config(state=tk.DISABLED if estimated_enabled else tk.NORMAL)

    def update_tracker_param_state(self, *args):
        method = (self.tracking_method_var.get() or "").lower()

        bt_widgets = list(getattr(self, "bt_widgets", []))
        dh_widgets = list(getattr(self, "dh_widgets", []))

        if method == "bytetrack":
            for w in bt_widgets:
                try:
                    w.configure(state="normal")
                except tk.TclError:
                    pass
            for w in dh_widgets:
                try:
                    w.configure(state="disabled")
                except tk.TclError:
                    pass
        elif method in ("deepsort", "hungarian"):
            for w in bt_widgets:
                try:
                    w.configure(state="disabled")
                except tk.TclError:
                    pass
            for w in dh_widgets:
                try:
                    w.configure(state="normal")
                except tk.TclError:
                    pass
        else:
            for w in bt_widgets + dh_widgets:
                try:
                    w.configure(state="normal")
                except tk.TclError:
                    pass

    def _make_time_entries_editable(self):
        for entry in (self.start_time_str, self.end_time_str):
            try:
                entry.config(state=tk.NORMAL)
            except tk.TclError:
                pass

    def _validate_time_entry(self, proposed: str, widget_name: str) -> bool:
        if proposed == "":
            return True

        if not re.fullmatch(r"[0-9:\-\s]*", proposed):
            return False

        return True

    def _handle_invalid_time(self, widget_name: str) -> None:
        try:
            widget = self.nametowidget(widget_name)
        except (KeyError, tk.TclError):
            messagebox.showwarning(
                "警告",
                "start_time_str/end_time_str は 'YYYY-MM-DD HH:MM:SS' または 'HH:MM:SS' 形式で入力してください。"
            )
            return

        label = "start_time_str" if widget is self.start_time_str else "end_time_str"
        messagebox.showwarning(
            "警告",
            f"{label} は 'YYYY-MM-DD HH:MM:SS' または 'HH:MM:SS' 形式で入力してください。"
        )
        try:
            widget.bell()
        except tk.TclError:
            pass

    def _validate_target_classes_entry(self, proposed: str, widget_name: str) -> bool:
        if proposed == "":
            return True

        if not re.fullmatch(r"[0-7,\s]*", proposed):
            return False

        parts = proposed.split(",")
        valid_tokens = {"0", "1", "2", "3", "4", "5", "6", "7"}
        for index, part in enumerate(parts):
            stripped = part.strip()
            if not stripped:
                if index == len(parts) - 1:
                    continue
                return False
            if stripped not in valid_tokens:
                return False

        return True

    def _handle_invalid_target_classes(self, widget_name: str) -> None:
        try:
            widget = self.nametowidget(widget_name)
        except (KeyError, tk.TclError):
            messagebox.showwarning("警告", "対象クラスは0～7までの数字をカンマ区切りで入力してください。")
            return

        messagebox.showwarning("警告", "対象クラスは0～7までの数字をカンマ区切りで入力してください。")
        try:
            widget.bell()
        except tk.TclError:
            pass

    @staticmethod
    def _is_valid_time_format(value: str) -> bool:
        if not value:
            return False

        value = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%H:%M:%S"):
            try:
                datetime.strptime(value, fmt)
                return True
            except ValueError:
                continue

        return False

    # -------------- File pickers --------------
    def browse_video_dir(self):
        path = filedialog.askdirectory(title="動画フォルダを選択")
        if path:
            self.video_dir.delete(0, tk.END)
            self.video_dir.insert(0, path)

    def browse_output_dir(self):
        path = filedialog.askdirectory(title="出力フォルダを選択")
        if path:
            self.output_dir.delete(0, tk.END)
            self.output_dir.insert(0, path)

    def reset_defaults(self):
        self.values = _migrate_legacy_values(DEFAULTS.copy())
        self._make_time_entries_editable()
        for w in [self.target_classes, self.start_time_str, self.end_time_str, self.video_dir, self.output_dir]:
            w.delete(0, tk.END)
        self.populate_fields()

    def load_config(self):
        path = filedialog.askopenfilename(
            title="設定ファイルを開く",
            filetypes=[("JSON files","*.json")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.values = _migrate_legacy_values({**DEFAULTS, **data})

            self._make_time_entries_editable()
            for w in [self.target_classes, self.start_time_str, self.end_time_str, self.video_dir, self.output_dir]:
                w.delete(0, tk.END)
            self.populate_fields()
            messagebox.showinfo("読み込み完了", f"{os.path.basename(path)} を読み込みました。")
        except Exception as e:
            messagebox.showerror("エラー", f"設定の読み込みに失敗しました:\n{e}")

    def save_config(self):
        try:
            cfg = {}
            cfg["crossing_system"] = int(self.crossing_system.get())

            # line & p1-4 & polygon & ocr_box from self.values (source of truth)
            cfg["line_start"] = self.values["line_start"]
            cfg["line_end"]   = self.values["line_end"]
            cfg["p1"] = self.values["p1"]
            cfg["p2"] = self.values["p2"]
            cfg["p3"] = self.values["p3"]
            cfg["p4"] = self.values["p4"]
            cfg["exclude_polygon"] = self.values["exclude_polygon"]
            cfg["ocr_box"] = self.values["ocr_box"]

            cfg["crossing_judgment_pattern"] = int(self.crossing_judgment_pattern.get())
            cfg["tracking_method"] = "bytetrack"
            selected_model = self.yolo_model.get()
            cfg["yolo_model"] = selected_model
            if isinstance(selected_model, str) and selected_model.startswith("yolo11") and len(selected_model) >= 7:
                cfg["yolo11_size"] = selected_model[6:]
            else:
                cfg["yolo11_size"] = DEFAULTS["yolo11_size"]

            raw_classes = [part.strip() for part in self.target_classes.get().split(",") if part.strip()]
            if not raw_classes:
                messagebox.showwarning("警告", "対象クラスは0～7までの数字をカンマ区切りで入力してください。")
                self.target_classes.focus_set()
                return
            classes = []
            for item in raw_classes:
                if not re.fullmatch(r"\d+", item):
                    messagebox.showwarning("警告", "対象クラスは0～7までの数字をカンマ区切りで入力してください。")
                    self.target_classes.focus_set()
                    return
                value = int(item)
                if not 0 <= value <= 7:
                    messagebox.showwarning("警告", "対象クラスは0～7までの数字をカンマ区切りで入力してください。")
                    self.target_classes.focus_set()
                    return
                classes.append(value)
            cfg["target_classes"] = classes

            cfg["confidence_threshold"] = float(self.confidence_var.get())
            cfg["distance_threshold"] = float(self.distance_threshold_var.get())
            cfg["iou_threshold"] = float(self.iou_threshold_var.get())
            cfg["max_iou_distance"] = float(self.max_iou_distance_var.get())
            cfg["frame_skip"] = int(self.frame_skip_var.get())
            cfg["frame_rate_original"] = float(self.frame_rate_original_var.get())
            cfg["std_acc"] = float(self.std_acc_var.get())
            cfg["x_std_meas"] = float(self.x_std_meas_var.get())
            cfg["y_std_meas"] = float(self.y_std_meas_var.get())
            cfg["max_age"] = int(self.max_age_var.get())
            cfg["n_init"] = int(self.n_init_var.get())
            cfg["nn_budget"] = int(self.nn_budget_var.get())
            cfg["congestion_calculation_interval"] = int(self.congestion_interval_var.get())
            cfg["enable_congestion"] = bool(self.enable_congestion_var.get())

            # -------- ByteTrack パラメータの必須チェック --------
            tm = (cfg.get("tracking_method") or "").lower()

            bt_high_str = self.bt_high_var.get().strip()
            bt_low_str = self.bt_low_var.get().strip()
            bt_match_str = self.bt_match_var.get().strip()
            bt_buffer_str = self.bt_buffer_var.get().strip()

            if tm == "bytetrack":
                # 1) 空欄チェック（ByteTrack に必要十分な 4 項目）
                if (
                    not bt_high_str
                    or not bt_low_str
                    or not bt_match_str
                    or not bt_buffer_str
                ):
                    messagebox.showwarning(
                        "警告",
                        "トラッキング手法で ByteTrack を選択している場合、\n"
                        "track_thresh / min_conf /\n"
                        "match_thresh / track_buffer をすべて入力してください。"
                    )
                    try:
                        if not bt_high_str:
                            self.bt_high_entry.focus_set()
                        elif not bt_low_str:
                            self.bt_low_entry.focus_set()
                        elif not bt_match_str:
                            self.bt_match_entry.focus_set()
                        else:
                            self.bt_buffer_entry.focus_set()
                    except tk.TclError:
                        pass
                    return  # 解析も起動しない

                # 2) 数値チェック
                try:
                    bt_high_val   = float(bt_high_str)
                    bt_low_val    = float(bt_low_str)
                    bt_match_val  = float(bt_match_str)
                    bt_buffer_val = int(bt_buffer_str)
                except ValueError:
                    messagebox.showwarning(
                        "警告",
                        "ByteTrack の各パラメータは数値で入力してください。\n"
                        "例: track_thresh=0.5, min_conf=0.3,\n"
                        "    match_thresh=0.8, track_buffer=30"
                    )
                    try:
                        self.bt_high_entry.focus_set()
                    except tk.TclError:
                        pass
                    return  # 解析も起動しない

                # 必須パラメータとして cfg に保存
                cfg["bt_track_high_thresh"] = bt_high_val   # → track_thresh
                cfg["bt_track_low_thresh"]  = bt_low_val    # → min_conf
                cfg["bt_match_thresh"]      = bt_match_val  # → match_thresh
                cfg["bt_track_buffer"]      = bt_buffer_val  # → track_buffer

            else:
                # ByteTrack 以外を選択している場合は、入力されていれば保存する程度にとどめる
                if bt_high_str:
                    cfg["bt_track_high_thresh"] = float(bt_high_str)
                if bt_low_str:
                    cfg["bt_track_low_thresh"] = float(bt_low_str)
                if bt_match_str:
                    cfg["bt_match_thresh"] = float(bt_match_str)
                if bt_buffer_str:
                    cfg["bt_track_buffer"] = int(bt_buffer_str)

            cfg["timestamp_ocr"] = bool(self.timestamp_ocr_var.get())
            cfg["show_estimated_time"] = bool(self.show_estimated_time_var.get())
            cfg["preview"] = True

            start_time = self.start_time_str.get().strip()
            end_time = self.end_time_str.get().strip()

            if not self._is_valid_time_format(start_time):
                messagebox.showwarning(
                    "警告",
                    "start_time_str は 'YYYY-MM-DD HH:MM:SS' または 'HH:MM:SS' 形式で入力してください。"
                )
                try:
                    self.start_time_str.focus_set()
                except tk.TclError:
                    pass
                return
            if not self._is_valid_time_format(end_time):
                messagebox.showwarning(
                    "警告",
                    "end_time_str は 'YYYY-MM-DD HH:MM:SS' または 'HH:MM:SS' 形式で入力してください。"
                )
                try:
                    self.end_time_str.focus_set()
                except tk.TclError:
                    pass
                return

            cfg["start_time_str"] = start_time
            cfg["end_time_str"] = end_time
            cfg["video_dir"] = self.video_dir.get().strip()
            cfg["output_dir"] = self.output_dir.get().strip()
            cfg["search_subdirs"] = bool(self.search_subdirs_var.get())

            if not cfg["output_dir"]:
                messagebox.showwarning("警告", "output_dir を入力してください。")
                try:
                    self.output_dir.focus_set()
                except tk.TclError:
                    pass
                return

            output_dir = cfg["output_dir"]
            if not os.path.isabs(output_dir):
                output_dir = os.path.abspath(output_dir)

            os.makedirs(output_dir, exist_ok=True)
            path = os.path.join(output_dir, "analyzer_config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            messagebox.showinfo("保存完了", f"{os.path.basename(path)} を {output_dir} に保存しました。")

            self._launch_aicount(cfg, path)

        except Exception as e:
            messagebox.showerror("エラー", f"保存に失敗しました:\n{e}")

    def _launch_aicount(self, cfg, config_path):
        cmd_args = []
        device = cfg.get("yolo_device", "auto") or "auto"
        exe = _find_aicount_exe()
        if exe:
            try:
                out = subprocess.check_output([exe, "--probe-device"], stderr=subprocess.STDOUT, timeout=5)
                info = json.loads(out.decode("utf-8", errors="ignore"))
                device = "cuda" if info.get("cuda") else "cpu"
            except Exception:
                pass

        # --config-json は AICount 独自設定としてのみ解釈し、
        # Ultralytics/YOLO の引数としては扱わない運用とする。
        cmd_args.extend([
            "--device", device,
            "--config-json", json.dumps(cfg, ensure_ascii=False),
            "--config", str(config_path),
        ])

        launch_aicount_with_args(cmd_args, master=self)

    # -------------- Picking logic --------------
    def _read_middle_frame(self, video_path):
        """動画の中央付近のフレームを1枚だけ高速に読み込む。"""
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError("動画を開けませんでした。")

        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

            # 総フレーム数が取得できる場合は中央フレームにシーク
            if total > 0:
                idx = max(total // 2, 0)
                # フレーム番号でシーク（順次読み込みより圧倒的に高速）
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()

                # 万が一失敗した場合は1/3あたりも試す（保険）
                if (not ok) or frame is None:
                    fallback_idx = max(total // 3, 0)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, fallback_idx)
                    ok, frame = cap.read()
            else:
                # フレーム数が取れない場合は先頭フレームのみ読む
                ok, frame = cap.read()

            if not ok or frame is None:
                raise RuntimeError("代表フレームを読み込めませんでした。")

            return frame

        finally:
            cap.release()

    def _ask_video(self):
        return filedialog.askopenfilename(
            title="動画ファイルを選択",
            filetypes=[("Video files", "*.mp4 *.MP4 *.avi *.AVI *.mkv *.MKV *.mts *.MTS *.asf *.ASF")]
        )

    def pick_line_start_end(self):
        path = self._ask_video()
        if not path:
            return
        try:
            frame = self._read_middle_frame(path)
        except Exception as e:
            messagebox.showerror("エラー", str(e)); return

        window = "座標入力: line_start / line_end (2クリック)"
        _create_fullscreen_window(window)
        disp = frame.copy()
        clicks = []
        cancelled = False

        def redraw():
            nonlocal disp
            disp = frame.copy()
            for i, (x,y) in enumerate(clicks):
                cv2.circle(disp, (x,y), 6, (0,255,255), -1)
            if len(clicks) >= 2:
                cv2.line(disp, clicks[0], clicks[1], (0,0,255), 2)
            cv2.imshow(window, disp)

        def on_mouse(e, x, y, flags, param):
            if e == cv2.EVENT_LBUTTONDOWN:
                clicks.append((x,y))
                redraw()

        cv2.setMouseCallback(window, on_mouse)
        redraw()

        awaiting_confirmation = False
        confirmed = False

        while True:
            self._process_events()
            if cancelled or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                cancelled = True
                clicks = []
                break
            key = cv2.waitKey(10) & 0xFF
            if key == 27:
                cancelled = True
                clicks = []
                break
            if len(clicks) >= 2 and not awaiting_confirmation:
                awaiting_confirmation = True
                ok = confirm_ok_retry("確認", "line_start/line_end を確定しますか？")
                if ok:
                    (x1,y1),(x2,y2) = clicks[:2]
                    self.values["line_start"] = [int(x1), int(y1)]
                    self.values["line_end"] = [int(x2), int(y2)]
                    self.line_coords_var.set(
                        " / ".join(
                            [
                                f"line_start=({x1},{y1})",
                                f"line_end=({x2},{y2})",
                            ]
                        )
                    )
                    confirmed = True
                    break
                clicks = []
                redraw()
                awaiting_confirmation = False

        try:
            cv2.destroyWindow(window)
        except Exception:
            pass
        if cancelled or not confirmed:
            return

    def pick_p1_p4(self):
        path = self._ask_video()
        if not path:
            return
        try:
            frame = self._read_middle_frame(path)
        except Exception as e:
            messagebox.showerror("エラー", str(e)); return

        window = "座標入力: p1→p2→p3→p4 (4クリック)"
        _create_fullscreen_window(window)
        disp = frame.copy()
        clicks = []
        cancelled = False

        def redraw():
            nonlocal disp
            disp = frame.copy()
            if len(clicks) >= 1:
                cv2.circle(disp, clicks[0], 6, (0,255,255), -1)
            if len(clicks) >= 2:
                cv2.line(disp, clicks[0], clicks[1], (0,0,255), 2)
            if len(clicks) >= 3:
                cv2.line(disp, clicks[1], clicks[2], (255,0,0), 2)
            if len(clicks) >= 4:
                cv2.line(disp, clicks[2], clicks[3], (0,255,255), 2)
                cv2.line(disp, clicks[3], clicks[0], (255,0,255), 2)
            cv2.imshow(window, disp)

        def on_mouse(e, x, y, flags, param):
            if e == cv2.EVENT_LBUTTONDOWN:
                if len(clicks) < 4:
                    clicks.append((x,y))
                    redraw()

        cv2.setMouseCallback(window, on_mouse)
        redraw()

        awaiting_confirmation = False
        confirmed = False

        while True:
            self._process_events()
            if cancelled or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                cancelled = True
                clicks = []
                break
            key = cv2.waitKey(10) & 0xFF
            if key == 27:
                cancelled = True
                clicks = []
                break
            if len(clicks) >= 4 and not awaiting_confirmation:
                awaiting_confirmation = True
                ok = confirm_ok_retry("確認", "p1〜p4 を確定しますか？")
                if ok:
                    (x1,y1),(x2,y2),(x3,y3),(x4,y4) = clicks[:4]
                    self.values["p1"] = [int(x1), int(y1)]
                    self.values["p2"] = [int(x2), int(y2)]
                    self.values["p3"] = [int(x3), int(y3)]
                    self.values["p4"] = [int(x4), int(y4)]
                    self.p1234_coords_var.set(
                        " / ".join(
                            [
                                f"p1=({x1},{y1})",
                                f"p2=({x2},{y2})",
                                f"p3=({x3},{y3})",
                                f"p4=({x4},{y4})",
                            ]
                        )
                    )
                    confirmed = True
                    break
                clicks = []
                redraw()
                awaiting_confirmation = False

        try:
            cv2.destroyWindow(window)
        except Exception:
            pass
        if cancelled or not confirmed:
            return

    def pick_exclude_polygon(self):
        path = self._ask_video()
        if not path:
            return
        try:
            frame = self._read_middle_frame(path)
        except Exception as e:
            messagebox.showerror("エラー", str(e)); return

        window = "座標入力: 除外ポリゴン（クリックで頂点追加）"
        _create_fullscreen_window(window)
        disp = frame.copy()
        clicks = []
        cancelled = False
        finished = False

        ctl = tk.Toplevel(self)
        ctl.title("操作")
        ctl.resizable(False, False)
        mark_ctl_closing = _enforce_dialog_constraints(ctl)
        tk.Label(
            ctl,
            text="クリックで頂点を追加します。\n『指定終了』で確定します。",
            padx=12,
            pady=8,
        ).pack()
        btn_area = tk.Frame(ctl)
        btn_area.pack(padx=10, pady=6)

        def reset_points():
            nonlocal clicks
            clicks = []
            redraw()

        def finish_polygon():
            nonlocal clicks, finished
            if len(clicks) < 3:
                messagebox.showwarning("警告", "3点以上指定してください。")
                return
            pts = [[int(x), int(y)] for (x, y) in clicks]
            if _polygon_self_intersects(pts):
                messagebox.showwarning("警告", "ポリゴンがクロスしています。もう一度指定してください。")
                reset_points()
                return
            self.values["exclude_polygon"] = pts
            self.exclude_polygon_var.set(
                "exclude_polygon=" + point_list_to_str(pts)
            )
            finished = True

        def cancel():
            nonlocal cancelled, clicks
            cancelled = True
            clicks = []
            try:
                cv2.destroyWindow(window)
            except Exception:
                pass
            mark_ctl_closing()
            try:
                ctl.destroy()
            except Exception:
                pass

        ctl.protocol("WM_DELETE_WINDOW", lambda: None)
        tk.Button(btn_area, text="もう一度", width=10, command=reset_points).pack(side="left", padx=4)
        tk.Button(btn_area, text="指定終了", width=10, command=finish_polygon).pack(side="left", padx=4)
        tk.Button(btn_area, text="キャンセル", width=10, command=cancel).pack(side="left", padx=4)

        def redraw():
            nonlocal disp
            disp = frame.copy()
            for i, (x,y) in enumerate(clicks):
                color = (0,255,255) if i == 0 else (255,165,0)
                cv2.circle(disp, (x,y), 5, color, -1)
            if len(clicks) >= 2:
                for i in range(len(clicks)-1):
                    cv2.line(disp, clicks[i], clicks[i+1], (255,165,0), 2)
                if len(clicks) >= 3:
                    cv2.line(disp, clicks[-1], clicks[0], (0,255,0), 2)
            cv2.imshow(window, disp)

        def on_mouse(e, x, y, flags, param):
            if e == cv2.EVENT_LBUTTONDOWN:
                clicks.append((x,y))
                redraw()

        cv2.setMouseCallback(window, on_mouse)
        redraw()

        while True:
            self._process_events()
            if finished:
                break
            if cancelled:
                clicks = []
                break
            if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                cancelled = True
                clicks = []
                break
            key = cv2.waitKey(10) & 0xFF
            if key == 27:
                cancelled = True
                clicks = []
                break

        try:
            cv2.destroyWindow(window)
        except Exception:
            pass
        mark_ctl_closing()
        try:
            ctl.destroy()
        except Exception:
            pass
        if cancelled or not finished:
            return

    def pick_ocr_box(self):
        path = self._ask_video()
        if not path:
            return
        try:
            frame = self._read_middle_frame(path)
        except Exception as e:
            messagebox.showerror("エラー", str(e)); return

        window = "座標入力: OCRボックス 左上→右下 (2クリック)"
        _create_fullscreen_window(window)
        disp = frame.copy()
        clicks = []
        cancelled = False

        def redraw():
            nonlocal disp
            disp = frame.copy()
            for i, (x,y) in enumerate(clicks):
                cv2.circle(disp, (x,y), 6, (0,255,255), -1)
            if len(clicks) == 2:
                (x1,y1),(x2,y2) = clicks
                cv2.rectangle(disp, (x1,y1), (x2,y2), (0,255,255), 2)
            cv2.imshow(window, disp)

        def on_mouse(e, x, y, flags, param):
            if e == cv2.EVENT_LBUTTONDOWN:
                if len(clicks) < 2:
                    clicks.append((x,y))
                    redraw()

        cv2.setMouseCallback(window, on_mouse)
        redraw()

        awaiting_confirmation = False
        confirmed = False

        while True:
            self._process_events()
            if cancelled or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                cancelled = True
                clicks = []
                break
            key = cv2.waitKey(10) & 0xFF
            if key == 27:
                cancelled = True
                clicks = []
                break
            if len(clicks) >= 2 and not awaiting_confirmation:
                awaiting_confirmation = True
                ok = confirm_ok_retry("確認", "OCRボックスを確定しますか？")
                if ok:
                    (x1,y1),(x2,y2) = clicks[:2]
                    x1_, x2_ = sorted([int(x1), int(x2)])
                    y1_, y2_ = sorted([int(y1), int(y2)])
                    self.values["ocr_box"] = [x1_, y1_, x2_, y2_]
                    self.ocr_box_var.set(f"ocr_box={x1_}, {y1_}, {x2_}, {y2_}")
                    confirmed = True
                    break
                clicks = []
                redraw()
                awaiting_confirmation = False

        try:
            cv2.destroyWindow(window)
        except Exception:
            pass
        if cancelled or not confirmed:
            return

if __name__ == "__main__":
    app = ConfigUI()
    app.mainloop()
