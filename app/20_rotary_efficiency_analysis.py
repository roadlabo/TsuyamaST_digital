import cv2
import pandas as pd
import numpy as np
import math
from ultralytics import YOLO
from datetime import datetime, timedelta
from collections import Counter
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path
import pytesseract
import re

# ================= 設定エリア =================
VIDEO_PATH = '1111long.mp4'
SHOW_VIDEO = True
OUTPUT_DIR = 'analysis_output'

# ロータリー領域（既存流用）
ZONE_POLYGON = np.array([
    (390, 106), (365, 128), (306, 188), (269, 277), (233, 421),
    (251, 587), (253, 712), (1071, 711), (1229, 664), (1275, 517),
    (1257, 434), (1216, 323), (1122, 256), (869, 175), (597, 92),
    (489, 87), (398, 91), (391, 103)
], np.int32)

# 出口ライン（既存流用）
LINE_EXIT_START = (352, 352)
LINE_EXIT_END   = (1085, 576)

# 停車帯領域（None の場合はゾーンチェックをスキップし移動量のみで判定）
# 要実測 - 実際の映像を確認して座標を設定してください
PARKING_ZONE_POLYGON = None     # TODO: 要実測
# 例: PARKING_ZONE_POLYGON = np.array([(400,300),(700,300),(700,600),(400,600)], np.int32)

# 追跡パラメータ（既存流用）
PATIENCE_SECONDS      = 5.0
REID_MAX_TIME         = 60.0
REID_MAX_DISTANCE     = 15.0
DUPLICATE_IOU_THRESHOLD = 0.7

# 停車判定パラメータ
PARK_MOVEMENT_THRESHOLD   = 15  # px：これ未満の移動量を「停車中」とみなす
PARK_CONFIRMATION_SECONDS = 3   # 秒：この時間停車が続いたら is_parked=True

# 運用効率指標パラメータ
ROTARY_CAPACITY = 10            # 名目停車可能台数
BASE_STAY_TIME  = 60            # 基準滞在時間（秒）
WINDOW_SECONDS  = 300           # 実効容量計算用スライディングウィンドウ幅（秒 = 5分）
C_NOMINAL = 3600 * ROTARY_CAPACITY / BASE_STAY_TIME  # = 600 台/時

# サイネージ制御閾値
THRESHOLD_HIGH_LOAD        = 7     # N_t がこれ以上なら台数過多
THRESHOLD_ALPHA_NORMAL     = 0.85  # α ≥ 0.85 → 正常
THRESHOLD_ALPHA_LIGHT      = 0.70  # 0.70 ≤ α < 0.85 → 軽度混雑
THRESHOLD_ALPHA_CONGESTED  = 0.55  # 0.55 ≤ α < 0.70 → 混雑 / < 0.55 → 深刻混雑

# 記録・保存間隔（既存流用）
ANALYSIS_INTERVAL = 1.0
SAVE_INTERVAL     = 30.0

# OCR設定（既存流用）
TIME_OCR_REGION = (10, 10, 350, 50)
# ==========================================================


# ============================================================
# Section 3: ユーティリティ関数（10_realtime_analysis.py からコピー）
# ============================================================

def preprocess_frame(frame):
    gamma = 0.7
    inv_gamma = 1.0 / gamma
    table = np.array([(i / 255.0) ** inv_gamma * 255 for i in range(256)]).astype("uint8")
    frame = cv2.LUT(frame, table)

    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    cl = clahe.apply(l)
    lab = cv2.merge((cl, a, b))
    frame = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    v = np.where(v > 230, 230, v)
    hsv = cv2.merge((h, s, v))
    frame = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    return frame


def extract_lights(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    _, _, v = cv2.split(hsv)
    _, mask = cv2.threshold(v, 230, 255, cv2.THRESH_BINARY)
    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.dilate(mask, kernel, iterations=2)
    return mask


def find_light_pairs(mask):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    lights = []
    for cnt in contours:
        if cv2.contourArea(cnt) < 20:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        lights.append((x + w // 2, y + h // 2))

    pairs = []
    used = set()
    for i in range(len(lights)):
        if i in used:
            continue
        for j in range(i + 1, len(lights)):
            if j in used:
                continue
            (x1, y1), (x2, y2) = lights[i], lights[j]
            if abs(y1 - y2) > 40:
                continue
            dist = abs(x1 - x2)
            if not (20 < dist < 350):
                continue
            pairs.append(((x1, y1), (x2, y2)))
            used.add(i)
            used.add(j)
            break
    return pairs


def estimate_car_box(pair):
    (x1, y1), (x2, y2) = pair
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    width_est = abs(x1 - x2) * 2.0
    height_est = width_est * 0.6
    x = cx
    y = cy + height_est * 0.15
    return (int(x), int(y), int(width_est), int(height_est))


def get_color(duration_sec):
    if duration_sec < 60:
        return (255, 255, 255)
    elif duration_sec < 180:
        return (0, 140, 255)
    else:
        return (0, 0, 255)


def calculate_distance(p1, p2):
    return math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)


def is_intersecting(p1, p2, l1, l2):
    def ccw(A, B, C):
        return (C[1] - A[1]) * (B[0] - A[0]) > (B[1] - A[1]) * (C[0] - A[0])
    return ccw(p1, l1, l2) != ccw(p2, l1, l2) and ccw(p1, p2, l1) != ccw(p1, p2, l2)


def calculate_iou(box1, box2):
    x1, y1, w1, h1 = box1
    x2, y2, w2, h2 = box2
    b1x1, b1y1 = x1 - w1 / 2, y1 - h1 / 2
    b1x2, b1y2 = x1 + w1 / 2, y1 + h1 / 2
    b2x1, b2y1 = x2 - w2 / 2, y2 - h2 / 2
    b2x2, b2y2 = x2 + w2 / 2, y2 + h2 / 2
    inter_w = max(0, min(b1x2, b2x2) - max(b1x1, b2x1))
    inter_h = max(0, min(b1y2, b2y2) - max(b1y1, b2y1))
    inter_area = inter_w * inter_h
    union_area = w1 * h1 + w2 * h2 - inter_area
    if union_area == 0:
        return 0.0
    return inter_area / union_area


def extract_date_from_filename(video_path):
    filename = Path(video_path).stem
    match = re.match(r'^(\d{2})(\d{2})', filename)
    if match:
        return f"2025-{match.group(1)}-{match.group(2)}"
    parts = filename.split('_')
    for part in parts:
        if len(part) >= 6 and part[:6].isdigit():
            yy, mm, dd = part[0:2], part[2:4], part[4:6]
            return f"20{yy}-{mm}-{dd}"
    return None


def extract_time_from_frame(frame, region=None):
    if region:
        x, y, w, h = region
        roi = frame[y:y + h, x:x + w]
    else:
        roi = frame
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)
    custom_config = r'--oem 3 --psm 7'
    text = pytesseract.image_to_string(binary, config=custom_config).strip()
    match = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', text)
    if match:
        try:
            return datetime.strptime(f"{match.group(1)}:{match.group(2)}:{match.group(3)}", "%H:%M:%S").time()
        except ValueError:
            pass
    match = re.search(r'(\d{1,2}):(\d{2})', text)
    if match:
        try:
            return datetime.strptime(f"{match.group(1)}:{match.group(2)}", "%H:%M").time()
        except ValueError:
            pass
    return None


def get_video_start_time(cap, date_str, ocr_region=None, sample_frames=5):
    if not date_str:
        print("⚠️  日付情報がないため、現在時刻を使用します")
        return datetime.now()

    original_pos = cap.get(cv2.CAP_PROP_POS_FRAMES)
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    detected_times = []
    for i in range(sample_frames):
        success, frame = cap.read()
        if not success:
            break
        time_obj = extract_time_from_frame(frame, ocr_region)
        if time_obj:
            detected_times.append(time_obj)
            print(f"  フレーム{i}: {time_obj.strftime('%H:%M:%S')} を検出")
    cap.set(cv2.CAP_PROP_POS_FRAMES, original_pos)

    if detected_times:
        most_common_time = Counter(detected_times).most_common(1)[0][0]
        start_datetime = datetime.combine(
            datetime.strptime(date_str, "%Y-%m-%d").date(),
            most_common_time
        )
        print(f"✅ 動画開始時刻: {start_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
        return start_datetime
    else:
        print("⚠️  OCRで時刻を検出できませんでした。現在時刻を使用します")
        return datetime.now()


# ============================================================
# Section 4: RotaryAnalyzer クラス
# ============================================================

class RotaryAnalyzer:
    def __init__(self, output_dir="analysis_output", date_str=None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.date_str = date_str

        self.metrics_log = []       # realtime_metrics_{date}.csv 用
        self.vehicle_events = []    # vehicle_events_{date}.csv 用
        self.exit_timestamps = []   # alpha 計算用（退出時刻リスト）

        # 高負荷セッション測定
        self.high_load_active = False       # 現在高負荷セッション中かどうか
        self.high_load_session_stays = []   # セッション中に収集した avg_stay サンプル
        self.high_load_alpha = None         # セッション内 avg_stay から算出した α（None=未計測）

        self.last_analysis_time = None
        self.last_save_time = None

    def _get_signage_state(self, vehicle_count: int) -> str:
        """サイネージ制御状態を判定する（8台以上のときのみα評価）"""
        if vehicle_count >= THRESHOLD_HIGH_LOAD:
            return 'HIGH_LOAD'
        return 'NORMAL'  # 8台未満は通常状態

    def _update_high_load(self, vehicle_count: int, avg_stay: float):
        """
        高負荷セッション（Nt >= THRESHOLD_HIGH_LOAD）の測定を管理する。

        - Nt が閾値を超えた瞬間に新しいセッションを開始（蓄積リセット）。
        - セッション中は avg_stay サンプルを1秒ごとに蓄積し、
          セッション内平均から α_high = BASE_STAY_TIME / session_avg を算出する。
        - Nt が閾値を下回ったらセッション終了。high_load_alpha は None にリセット。
        """
        if vehicle_count >= THRESHOLD_HIGH_LOAD:
            if not self.high_load_active:
                # 新しいセッション開始 → リセット
                self.high_load_active = True
                self.high_load_session_stays = []
            if avg_stay > 0:
                self.high_load_session_stays.append(avg_stay)
            if self.high_load_session_stays:
                session_avg = sum(self.high_load_session_stays) / len(self.high_load_session_stays)
                self.high_load_alpha = BASE_STAY_TIME / session_avg
        else:
            if self.high_load_active:
                # セッション終了
                self.high_load_active = False
                self.high_load_session_stays = []
                self.high_load_alpha = None

    def _calculate_alpha(self, current_time: datetime, vehicle_count: int = 0, avg_stay: float = 0.0):
        """
        運用効率αを算出する。

        - 通常時（Nt < THRESHOLD_HIGH_LOAD）: スライディングウィンドウ方式
            α = C_effective / C_nominal = (3600 * N_out / T) / C_nominal
        - 高負荷時（Nt >= THRESHOLD_HIGH_LOAD）: 平均滞在時間方式
            α = BASE_STAY_TIME / セッション内平均滞在時間

        Returns: (alpha: float, n_out: int, alpha_window: float)
        """
        # スライディングウィンドウ alpha（常に計算・記録用）
        window_start = current_time - timedelta(seconds=WINDOW_SECONDS)
        self.exit_timestamps = [t for t in self.exit_timestamps if t >= window_start]
        n_out = len(self.exit_timestamps)
        c_effective = 3600 * n_out / WINDOW_SECONDS
        alpha_window = c_effective / C_NOMINAL if C_NOMINAL > 0 else 0.0

        # 高負荷セッション測定を更新
        self._update_high_load(vehicle_count, avg_stay)

        # α の選択: 高負荷セッション中は高負荷α、それ以外はウィンドウα
        alpha = self.high_load_alpha if self.high_load_active and self.high_load_alpha is not None else alpha_window

        return alpha, n_out, alpha_window

    def _update_parking_state(self, vid: int, current_pos: tuple,
                              v_data: dict, current_time: datetime, fps: float):
        """
        車両の停車状態を1フレームごとに更新する。
        位置変化が PARK_MOVEMENT_THRESHOLD 未満で PARK_CONFIRMATION_SECONDS 継続したら停車確定。
        停車帯ポリゴン（PARKING_ZONE_POLYGON）が設定されている場合はゾーン内チェックも行う。
        """
        dist = calculate_distance(current_pos, v_data['prev_pos'])
        required_frames = int(PARK_CONFIRMATION_SECONDS * fps) if fps > 0 else 90

        if dist < PARK_MOVEMENT_THRESHOLD:
            v_data['no_move_frames'] += 1
            if v_data['no_move_frames'] >= required_frames and not v_data['is_parked']:
                # 停車帯ゾーンチェック
                in_zone = True
                if PARKING_ZONE_POLYGON is not None:
                    in_zone = cv2.pointPolygonTest(PARKING_ZONE_POLYGON, current_pos, False) >= 0
                if in_zone:
                    v_data['is_parked'] = True
                    v_data['parking_start'] = current_time
        else:
            v_data['no_move_frames'] = 0
            if v_data['is_parked']:
                v_data['is_parked'] = False
                v_data['parking_start'] = None

    def record_metrics(self, timestamp: datetime, vehicle_count: int,
                       avg_stay_time: float, alpha: float, alpha_window: float,
                       n_out: int, signage_state: str):
        """1秒ごとのスナップショットを記録する"""
        # 高負荷セッション中の平均滞在時間（未計測時は None）
        session_avg = None
        if self.high_load_session_stays:
            session_avg = round(sum(self.high_load_session_stays) / len(self.high_load_session_stays), 2)

        self.metrics_log.append({
            'timestamp': timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            'vehicle_count': vehicle_count,
            'avg_stay_time': round(avg_stay_time, 2),
            'avg_stay_highload': session_avg,       # 高負荷セッション内の平均滞在時間
            'vehicle_throughput': n_out,
            'alpha': round(alpha, 4),               # 有効α（高負荷時は滞在時間ベース）
            'alpha_window': round(alpha_window, 4), # 参照用スライディングウィンドウα
            'high_load_mode': self.high_load_active,
            'signage_state': signage_state,
        })

    def record_vehicle_exit(self, vid: int, v_data: dict,
                            exit_time: datetime, exit_reason: str):
        """
        退出イベントを記録する。
        同時に exit_timestamps に退出時刻を追加してα計算に反映する。
        """
        self.exit_timestamps.append(exit_time)
        stay_time = (exit_time - v_data['start_time']).total_seconds()
        self.vehicle_events.append({
            'vehicle_id': vid,
            'type': v_data.get('type', 'unknown'),
            'entry_time': v_data['start_time'].strftime('%Y-%m-%d %H:%M:%S'),
            'exit_time': exit_time.strftime('%Y-%m-%d %H:%M:%S'),
            'stay_time_s': round(stay_time, 2),
            'entered_rotary_zone': v_data.get('entered_rotary_zone', False),
            # 既存CSV互換: 旧列名にも同じ値を出力
            'entered_via_line': v_data.get('entered_rotary_zone', False),
            'exit_reason': exit_reason,
        })

    def save_realtime_metrics(self, force=False):
        """realtime_metrics_{date}.csv に保存する"""
        if not self.metrics_log:
            return
        df = pd.DataFrame(self.metrics_log)
        suffix = self.date_str if self.date_str else 'unknown'
        path = self.output_dir / f'realtime_metrics_{suffix}.csv'
        df.to_csv(path, index=False)
        if force:
            print(f"💾 メトリクス最終保存: {path} ({len(self.metrics_log)} records)")

    def save_vehicle_events(self):
        """vehicle_events_{date}.csv に保存する"""
        if not self.vehicle_events:
            return
        df = pd.DataFrame(self.vehicle_events)
        suffix = self.date_str if self.date_str else 'unknown'
        path = self.output_dir / f'vehicle_events_{suffix}.csv'
        df.to_csv(path, index=False)

    def plot_metrics(self):
        """4パネルの解析グラフを生成して保存する"""
        if not self.metrics_log:
            return

        plt.rcParams['font.family'] = 'DejaVu Sans'
        plt.rcParams['axes.unicode_minus'] = False
        plt.rcParams['font.size'] = 11

        df = pd.DataFrame(self.metrics_log)
        df['timestamp'] = pd.to_datetime(df['timestamp'])

        suffix = f" ({self.date_str})" if self.date_str else ""

        # ガントチャートの有無でレイアウトを変える
        has_events = bool(self.vehicle_events)
        n_panels = 4 if has_events else 3
        height_ratios = [1, 1.2, 1, 2] if has_events else [1, 1.2, 1]

        fig, axes = plt.subplots(n_panels, 1, figsize=(16, 4 * n_panels),
                                 sharex=False,
                                 gridspec_kw={'height_ratios': height_ratios})
        ax1, ax2, ax3 = axes[0], axes[1], axes[2]
        ax4 = axes[3] if has_events else None

        # ── Panel 1: 滞在台数 Nt ──────────────────────────────
        ax1.step(df['timestamp'], df['vehicle_count'], where='post',
                 color='#1f77b4', linewidth=2)
        ax1.fill_between(df['timestamp'], df['vehicle_count'], step='post',
                         color='#1f77b4', alpha=0.2)
        ax1.axhline(y=THRESHOLD_HIGH_LOAD, color='red', linestyle='--',
                    linewidth=1.5, label=f'High Load ({THRESHOLD_HIGH_LOAD}台)')
        ax1.set_title(f'Vehicle Count in Rotary (Nt){suffix}', fontweight='bold')
        ax1.set_ylabel('Vehicles')
        ax1.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
        ax1.legend(loc='upper right', fontsize=9)
        ax1.grid(True, linestyle='--', alpha=0.5)

        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax1.xaxis.set_major_locator(mdates.AutoDateLocator())
        plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right')

        # ── Panel 2: 運用効率 α ──────────────────────────────
        ax2.plot(df['timestamp'], df['alpha'], color='#2ca02c', linewidth=2)

        # 閾値ラインと色分け塗りつぶし
        ax2.axhline(y=THRESHOLD_ALPHA_NORMAL,    color='#2ca02c', linestyle='--',
                    linewidth=1, label=f'Normal ({THRESHOLD_ALPHA_NORMAL})')
        ax2.axhline(y=THRESHOLD_ALPHA_LIGHT,     color='#ff7f0e', linestyle='--',
                    linewidth=1, label=f'Light ({THRESHOLD_ALPHA_LIGHT})')
        ax2.axhline(y=THRESHOLD_ALPHA_CONGESTED, color='#d62728', linestyle='--',
                    linewidth=1, label=f'Congested ({THRESHOLD_ALPHA_CONGESTED})')

        ax2.fill_between(df['timestamp'], df['alpha'], THRESHOLD_ALPHA_NORMAL,
                         where=(df['alpha'] >= THRESHOLD_ALPHA_NORMAL),
                         interpolate=True, color='#2ca02c', alpha=0.15)
        ax2.fill_between(df['timestamp'], df['alpha'], THRESHOLD_ALPHA_CONGESTED,
                         where=(df['alpha'] < THRESHOLD_ALPHA_NORMAL) & (df['alpha'] >= THRESHOLD_ALPHA_CONGESTED),
                         interpolate=True, color='#ff7f0e', alpha=0.15)
        ax2.fill_between(df['timestamp'], df['alpha'], 0,
                         where=(df['alpha'] < THRESHOLD_ALPHA_CONGESTED),
                         interpolate=True, color='#d62728', alpha=0.2)

        ax2.set_title(f'Operational Efficiency Index (α){suffix}', fontweight='bold')
        ax2.set_ylabel('α')
        ax2.set_ylim(bottom=0)
        ax2.legend(loc='upper right', fontsize=9)
        ax2.grid(True, linestyle='--', alpha=0.5)

        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha='right')

        # ── Panel 3: 退出台数（スライディングウィンドウ内） ──
        ax3.fill_between(df['timestamp'], df['vehicle_throughput'],
                         color='#9467bd', alpha=0.5)
        ax3.plot(df['timestamp'], df['vehicle_throughput'],
                 color='#9467bd', linewidth=1.5)
        ax3.set_title(
            f'Vehicle Throughput (exits within {WINDOW_SECONDS//60}min window){suffix}',
            fontweight='bold')
        ax3.set_ylabel('N_out')
        ax3.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
        ax3.grid(True, linestyle='--', alpha=0.5)

        ax3.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax3.xaxis.set_major_locator(mdates.AutoDateLocator())
        plt.setp(ax3.xaxis.get_majorticklabels(), rotation=30, ha='right')

        # ── Panel 4: ガントチャート ──────────────────────────
        if has_events and ax4 is not None:
            df_ev = pd.DataFrame(self.vehicle_events)
            df_ev['entry_time'] = pd.to_datetime(df_ev['entry_time'])
            df_ev['exit_time']  = pd.to_datetime(df_ev['exit_time'])
            df_ev = df_ev.sort_values('entry_time').reset_index(drop=True)

            for i, row in df_ev.iterrows():
                stay = row['stay_time_s']
                bar_color = '#1f77b4'
                if stay > 180: bar_color = '#ff7f0e'
                if stay > 300: bar_color = '#d62728'

                ax4.barh(y=i,
                         width=pd.Timedelta(seconds=stay),
                         left=row['entry_time'],
                         height=0.8,
                         color=bar_color,
                         edgecolor='black',
                         alpha=0.8)
                if stay > 10:
                    mid = row['entry_time'] + pd.Timedelta(seconds=stay / 2)
                    ax4.text(mid, i, f"{int(stay)}s",
                             va='center', ha='center',
                             color='white', fontsize=8, fontweight='bold')

            ax4.set_yticks(range(len(df_ev)))
            ax4.set_yticklabels([f"ID:{row['vehicle_id']}" for _, row in df_ev.iterrows()],
                                fontsize=8)
            ax4.invert_yaxis()
            ax4.set_title(f'Individual Stay Duration (Gantt){suffix}', fontweight='bold')
            ax4.set_ylabel('Vehicle ID')
            ax4.set_xlabel('Time')
            ax4.grid(True, axis='x', linestyle='--', alpha=0.5)
            ax4.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            plt.setp(ax4.xaxis.get_majorticklabels(), rotation=30, ha='right')

        plt.tight_layout()

        suffix_str = self.date_str if self.date_str else 'unknown'
        plot_path = self.output_dir / f'rotary_efficiency_{suffix_str}.png'
        plt.savefig(plot_path, dpi=150, bbox_inches='tight')
        plt.close()
        print(f"📈 グラフ保存: {plot_path}")

    def generate_html_report(self):
        """Plotly を使ったインタラクティブ HTML レポートを生成する"""
        try:
            import plotly.graph_objects as go
            from plotly.subplots import make_subplots
        except ImportError:
            print("⚠️  plotly が未インストールです。pip install plotly を実行してください。")
            return

        if not self.metrics_log:
            return

        df = pd.DataFrame(self.metrics_log)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        has_events = bool(self.vehicle_events)
        suffix = f" ({self.date_str})" if self.date_str else ""

        # ── ガントチャートの行数・高さ計算 ──────────────────────
        n_vehicles = len(self.vehicle_events) if has_events else 0
        # 1台あたりの高さ: データ数が多いほど縮小（最小15px、最大50px）
        row_px = max(15, min(50, 1200 // max(n_vehicles, 1)))
        gantt_height_px = max(300, n_vehicles * (row_px + 4)) if has_events else 0
        tick_font_size  = max(7, min(12, 250 // max(n_vehicles, 1)))

        n_rows       = 4 if has_events else 3
        panel_height = 260  # panels 1-3 各高さ(px)
        total_height = panel_height * 3 + gantt_height_px + 120  # spacing

        row_heights_frac = [panel_height] * 3 + ([gantt_height_px] if has_events else [])
        row_heights_norm = [v / sum(row_heights_frac) for v in row_heights_frac]

        subplot_titles = [
            f'Vehicle Count in Rotary (Nt){suffix}',
            f'Operational Efficiency Index (α){suffix}',
            f'Vehicle Throughput (exits within {WINDOW_SECONDS // 60}min window){suffix}',
        ] + ([f'Individual Stay Duration (Gantt){suffix}'] if has_events else [])

        fig = make_subplots(
            rows=n_rows, cols=1,
            shared_xaxes=False,
            row_heights=row_heights_norm,
            subplot_titles=subplot_titles,
            vertical_spacing=0.06,
        )

        # ── Panel 1: 滞在台数 Nt ────────────────────────────────
        fig.add_trace(go.Scatter(
            x=df['timestamp'], y=df['vehicle_count'],
            mode='lines',
            line=dict(color='#1f77b4', width=2, shape='hv'),
            fill='tozeroy', fillcolor='rgba(31,119,180,0.2)',
            name='Nt',
            hovertemplate='<b>時刻:</b> %{x|%H:%M:%S}<br><b>滞在台数:</b> %{y} 台<extra></extra>',
        ), row=1, col=1)
        fig.add_hline(
            y=THRESHOLD_HIGH_LOAD,
            line=dict(color='red', dash='dash', width=1.5),
            annotation_text=f'High Load ({THRESHOLD_HIGH_LOAD}台)',
            annotation_position='top right',
            row=1, col=1,
        )

        # ── Panel 2: 運用効率 α ─────────────────────────────────
        fig.add_trace(go.Scatter(
            x=df['timestamp'], y=df['alpha'],
            mode='lines',
            line=dict(color='#2ca02c', width=2),
            name='α',
            hovertemplate='<b>時刻:</b> %{x|%H:%M:%S}<br><b>α:</b> %{y:.4f}<extra></extra>',
        ), row=2, col=1)
        for thresh, color, label in [
            (THRESHOLD_ALPHA_NORMAL,    '#2ca02c', f'Normal ({THRESHOLD_ALPHA_NORMAL})'),
            (THRESHOLD_ALPHA_LIGHT,     '#ff7f0e', f'Light ({THRESHOLD_ALPHA_LIGHT})'),
            (THRESHOLD_ALPHA_CONGESTED, '#d62728', f'Congested ({THRESHOLD_ALPHA_CONGESTED})'),
        ]:
            fig.add_hline(
                y=thresh,
                line=dict(color=color, dash='dash', width=1),
                annotation_text=label,
                annotation_position='top right',
                row=2, col=1,
            )

        # ── Panel 3: スループット ───────────────────────────────
        fig.add_trace(go.Scatter(
            x=df['timestamp'], y=df['vehicle_throughput'],
            mode='lines',
            line=dict(color='#9467bd', width=1.5),
            fill='tozeroy', fillcolor='rgba(148,103,189,0.4)',
            name='N_out',
            hovertemplate='<b>時刻:</b> %{x|%H:%M:%S}<br><b>退出台数(window):</b> %{y} 台<extra></extra>',
        ), row=3, col=1)

        # ── Panel 4: ガントチャート ─────────────────────────────
        if has_events:
            df_ev = pd.DataFrame(self.vehicle_events)
            df_ev['entry_time'] = pd.to_datetime(df_ev['entry_time'])
            df_ev['exit_time']  = pd.to_datetime(df_ev['exit_time'])
            df_ev = df_ev.sort_values('entry_time').reset_index(drop=True)

            for _, row in df_ev.iterrows():
                stay = row['stay_time_s']
                if stay > 300:
                    color = '#d62728'
                elif stay > 180:
                    color = '#ff7f0e'
                else:
                    color = '#1f77b4'

                fig.add_trace(go.Bar(
                    x=[stay * 1000],          # ミリ秒単位（Plotly の datetime 軸に合わせる）
                    y=[f"ID:{int(row['vehicle_id'])}"],
                    base=[row['entry_time']],
                    orientation='h',
                    marker=dict(color=color, line=dict(color='black', width=0.5)),
                    opacity=0.85,
                    showlegend=False,
                    customdata=[[
                        int(row['vehicle_id']),
                        row['entry_time'].strftime('%H:%M:%S'),
                        row['exit_time'].strftime('%H:%M:%S'),
                        int(stay),
                    ]],
                    hovertemplate=(
                        '<b>Vehicle ID:</b> %{customdata[0]}<br>'
                        '<b>入場:</b> %{customdata[1]}<br>'
                        '<b>退場:</b> %{customdata[2]}<br>'
                        '<b>滞在時間:</b> %{customdata[3]} 秒<extra></extra>'
                    ),
                ), row=4, col=1)

            fig.update_yaxes(
                autorange='reversed',
                tickfont=dict(size=tick_font_size),
                title_text='Vehicle ID',
                row=4, col=1,
            )
            fig.update_xaxes(type='date', tickformat='%H:%M', row=4, col=1)

        # ── レイアウト共通設定 ───────────────────────────────────
        fig.update_layout(
            height=total_height,
            title_text=f'Rotary Efficiency Analysis{suffix}',
            title_font_size=20,
            showlegend=False,
            plot_bgcolor='white',
            paper_bgcolor='#f8f9fa',
            font=dict(family='Arial, sans-serif', size=12),
            hovermode='closest',
            margin=dict(l=90, r=60, t=100, b=60),
            bargap=0.15,
        )

        for r in range(1, n_rows + 1):
            fig.update_xaxes(
                showgrid=True, gridcolor='rgba(0,0,0,0.08)',
                tickformat='%H:%M',
                row=r, col=1,
            )
            fig.update_yaxes(
                showgrid=True, gridcolor='rgba(0,0,0,0.08)',
                row=r, col=1,
            )

        fig.update_yaxes(title_text='Vehicles', row=1, col=1)
        fig.update_yaxes(title_text='α', rangemode='tozero', row=2, col=1)
        fig.update_yaxes(title_text='N_out', row=3, col=1)

        suffix_str = self.date_str if self.date_str else 'unknown'
        html_path = self.output_dir / f'rotary_efficiency_{suffix_str}.html'
        fig.write_html(str(html_path), include_plotlyjs='cdn')
        print(f"🌐 HTMLレポート保存: {html_path}")

    def generate_summary(self):
        """解析サマリーをテキストファイルに保存する"""
        if not self.metrics_log:
            return

        df = pd.DataFrame(self.metrics_log)
        suffix = self.date_str if self.date_str else 'unknown'
        summary_path = self.output_dir / f'analysis_summary_{suffix}.txt'

        total_exits = len(self.vehicle_events)
        avg_alpha = df['alpha'].mean()
        max_nt = df['vehicle_count'].max()
        avg_nt = df['vehicle_count'].mean()

        state_dist = df['signage_state'].value_counts(normalize=True) * 100

        lines = [
            "=" * 60,
            "ロータリー運用効率 解析サマリー",
            "=" * 60,
            f"解析日付: {self.date_str or '不明'}",
            f"解析期間: {df['timestamp'].iloc[0]} 〜 {df['timestamp'].iloc[-1]}",
            f"総記録数: {len(df)} records",
            "",
            "【滞在台数】",
            f"  平均滞在台数: {avg_nt:.2f} 台",
            f"  最大滞在台数: {max_nt} 台",
            "",
            "【運用効率α】",
            f"  平均α: {avg_alpha:.4f}",
            f"  名目容量 C_nominal: {C_NOMINAL:.1f} 台/時",
            "",
            "【サイネージ状態の分布】",
        ]
        for state, pct in state_dist.items():
            lines.append(f"  {state}: {pct:.1f}%")

        lines += [
            "",
            f"【退出記録】",
            f"  退出確定台数: {total_exits} 台",
        ]

        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        print(f"\n📊 サマリー保存: {summary_path}")
        print('\n'.join(lines))


# ============================================================
# Section 5: main()
# ============================================================

def main():
    date_str = extract_date_from_filename(VIDEO_PATH)
    model = YOLO('yolo11n.pt')

    cap = cv2.VideoCapture(VIDEO_PATH)
    if not cap.isOpened():
        print(f"❌ 動画ファイルが開けません: {VIDEO_PATH}")
        return

    fps = cap.get(cv2.CAP_PROP_FPS)
    width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))

    print("🔍 動画から日時情報を取得中...")
    print(f"📅 日付: {date_str or '不明'}")
    START_TIME = get_video_start_time(cap, date_str, TIME_OCR_REGION)

    print(f"\n🚀 ロータリー運用効率解析システム 起動")
    print(f"📹 動画: {VIDEO_PATH}")
    print(f"🕐 開始時刻: {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📐 名目容量 C_nominal: {C_NOMINAL:.1f} 台/時")
    print(f"⏱️  観測窓: {WINDOW_SECONDS}秒 ({WINDOW_SECONDS//60}分)\n")

    vehicles = {}       # 追跡中の車両
    lost_vehicles = {}  # 一時ロスト中の車両
    id_map = {}
    fake_id_counter = 10000

    analyzer = RotaryAnalyzer(output_dir=OUTPUT_DIR, date_str=date_str)

    # サイネージ状態の色マップ
    SIGNAGE_COLORS = {
        'NORMAL':            (0, 220, 0),
        'LIGHT_CONGESTION':  (0, 200, 255),
        'CONGESTED':         (0, 130, 255),
        'SEVERE_CONGESTION': (0, 0, 230),
        'HIGH_LOAD':         (0, 0, 255),
    }

    while cap.isOpened():
        success, frame = cap.read()
        if not success:
            break

        current_frame = cap.get(cv2.CAP_PROP_POS_FRAMES)
        current_real_time = START_TIME + timedelta(seconds=current_frame / fps)

        processed = preprocess_frame(frame.copy())

        # ── YOLO 追跡 ──────────────────────────────────────
        results = model.track(
            processed, persist=True,
            conf=0.15, iou=0.5,
            classes=[2, 3, 5, 7],
            verbose=False
        )

        boxes, clss, track_ids = [], [], []
        if results[0].boxes.id is not None:
            track_ids = results[0].boxes.id.int().cpu().tolist()
            boxes     = results[0].boxes.xywh.cpu().tolist()
            clss      = results[0].boxes.cls.int().cpu().tolist()

        # ── Fake Car 検出 ──────────────────────────────────
        mask = extract_lights(processed)
        pairs = find_light_pairs(mask)
        fake_boxes = [estimate_car_box(p) for p in pairs]
        unique_fake_boxes = []
        for fx, fy, fw, fh in fake_boxes:
            if not any(abs(fx - x) < w / 2 and abs(fy - y) < h / 2 for x, y, w, h in boxes):
                unique_fake_boxes.append((fx, fy, fw, fh))

        # ── YOLO 重複除去 ──────────────────────────────────
        valid_detections = []
        for (x, y, w, h), raw_id, cls_id in zip(boxes, track_ids, clss):
            center = (int(x), int(y))
            if cv2.pointPolygonTest(ZONE_POLYGON, center, False) < 0:
                continue
            is_dup = False
            for j, (vx, vy, vw, vh, _, _) in enumerate(valid_detections):
                if calculate_iou((x, y, w, h), (vx, vy, vw, vh)) > DUPLICATE_IOU_THRESHOLD:
                    if w * h < vw * vh:
                        is_dup = True
                    else:
                        valid_detections.pop(j)
                    break
            if not is_dup:
                valid_detections.append((x, y, w, h, raw_id, cls_id))

        current_frame_ids = []

        # ── YOLO 検出の車両状態更新 ────────────────────────
        for (x, y, w, h, raw_id, cls_id) in valid_detections:
            center = (int(x), int(y))
            current_id = id_map.get(raw_id, raw_id)

            if current_id not in vehicles:
                matched_id, min_dist = None, float('inf')
                for lost_id, lost_info in lost_vehicles.items():
                    if (current_real_time - lost_info['lost_time']).total_seconds() > REID_MAX_TIME:
                        continue
                    d = calculate_distance(center, lost_info['lost_pos'])
                    if d < REID_MAX_DISTANCE and d < min_dist:
                        matched_id, min_dist = lost_id, d

                if matched_id is not None:
                    id_map[raw_id] = matched_id
                    current_id = matched_id
                    vehicles[current_id] = lost_vehicles[matched_id]['data']
                    vehicles[current_id]['finalized'] = False
                    del lost_vehicles[matched_id]
                else:
                    vehicles[current_id] = {
                        'start_time': current_real_time,
                        'last_seen': current_real_time,
                        'prev_pos': center,
                        'type': model.names[cls_id],
                        'finalized': False,
                        'bbox': (x, y, w, h),
                        'entered_rotary_zone': True,
                        'is_parked': False,
                        'parking_start': None,
                        'no_move_frames': 0,
                    }

            v_data = vehicles[current_id]

            if not v_data['finalized']:
                prev_pos = v_data['prev_pos']

                # ロータリー領域への侵入判定（ゾーン内に存在した時点で侵入扱い）
                if not v_data.get('entered_rotary_zone', False):
                    v_data['entered_rotary_zone'] = cv2.pointPolygonTest(ZONE_POLYGON, center, False) >= 0

                # 出口ライン通過判定
                if is_intersecting(prev_pos, center, LINE_EXIT_START, LINE_EXIT_END):
                    v_data['finalized'] = True
                    analyzer.record_vehicle_exit(current_id, v_data, current_real_time, 'Line')
                else:
                    # 停車判定・位置更新
                    analyzer._update_parking_state(current_id, center, v_data, current_real_time, fps)
                    v_data['last_seen'] = current_real_time
                    v_data['prev_pos'] = center
                    v_data['bbox'] = (x, y, w, h)

                    # 描画
                    if SHOW_VIDEO:
                        duration = (current_real_time - v_data['start_time']).total_seconds()
                        color = (0, 255, 165) if v_data['is_parked'] else get_color(duration)
                        x1, y1 = int(x - w / 2), int(y - h / 2)
                        x2, y2 = int(x + w / 2), int(y + h / 2)
                        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                        label = f"{int(duration)}s" + (" [P]" if v_data['is_parked'] else "")
                        cv2.putText(frame, label, (x1, y1 - 5),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

            current_frame_ids.append(current_id)

        # ── Fake Car の車両状態更新 ────────────────────────
        for fx, fy, fw, fh in unique_fake_boxes:
            center = (fx, fy)
            if cv2.pointPolygonTest(ZONE_POLYGON, center, False) < 0:
                continue
            if any(
                calculate_iou((fx, fy, fw, fh), vehicles[eid]['bbox']) > DUPLICATE_IOU_THRESHOLD
                for eid in current_frame_ids
                if eid in vehicles and 'bbox' in vehicles[eid]
            ):
                continue

            matched_id, min_dist = None, float('inf')
            for lost_id, lost_info in lost_vehicles.items():
                if (current_real_time - lost_info['lost_time']).total_seconds() > REID_MAX_TIME:
                    continue
                d = calculate_distance(center, lost_info['lost_pos'])
                if d < REID_MAX_DISTANCE and d < min_dist:
                    matched_id, min_dist = lost_id, d

            if matched_id is not None:
                current_id = matched_id
                vehicles[current_id] = lost_vehicles[matched_id]['data']
                vehicles[current_id].update({
                    'finalized': False,
                    'last_seen': current_real_time,
                    'prev_pos': center,
                    'bbox': (fx, fy, fw, fh),
                })
                del lost_vehicles[matched_id]
            else:
                fake_id_counter += 1
                current_id = fake_id_counter
                vehicles[current_id] = {
                    'start_time': current_real_time,
                    'last_seen': current_real_time,
                    'prev_pos': center,
                    'type': 'car_fake',
                    'finalized': False,
                    'bbox': (fx, fy, fw, fh),
                    'entered_rotary_zone': True,
                    'is_parked': False,
                    'parking_start': None,
                    'no_move_frames': 0,
                }

            current_frame_ids.append(current_id)

        # ── Lost 管理 ──────────────────────────────────────
        active_count = 0
        active_durations = []

        for vid in list(vehicles.keys()):
            v_data = vehicles[vid]

            if vid in current_frame_ids:
                if not v_data['finalized']:
                    active_count += 1
                    duration = (current_real_time - v_data['start_time']).total_seconds()
                    active_durations.append(duration)
                continue

            if v_data['finalized']:
                continue

            time_since_seen = (current_real_time - v_data['last_seen']).total_seconds()
            if time_since_seen <= PATIENCE_SECONDS:
                active_count += 1
                duration = (current_real_time - v_data['start_time']).total_seconds()
                active_durations.append(duration)
                continue

            lost_vehicles[vid] = {
                'data': v_data,
                'lost_time': v_data['last_seen'],
                'lost_pos': v_data['prev_pos'],
            }
            del vehicles[vid]

        # ── Timeout 退出 ──────────────────────────────────
        for vid in list(lost_vehicles.keys()):
            l_info = lost_vehicles[vid]
            if (current_real_time - l_info['lost_time']).total_seconds() > REID_MAX_TIME:
                analyzer.record_vehicle_exit(vid, l_info['data'], l_info['lost_time'], 'Timeout')
                del lost_vehicles[vid]

        # ── 1秒ごとのメトリクス記録 ───────────────────────
        avg_stay = sum(active_durations) / len(active_durations) if active_durations else 0.0
        alpha, n_out, alpha_window = analyzer._calculate_alpha(
            current_real_time, vehicle_count=active_count, avg_stay=avg_stay)
        signage_state = analyzer._get_signage_state(active_count)

        if analyzer.last_analysis_time is None:
            analyzer.last_analysis_time = current_real_time
            analyzer.last_save_time = current_real_time

        time_since_analysis = (current_real_time - analyzer.last_analysis_time).total_seconds()
        if time_since_analysis >= ANALYSIS_INTERVAL:
            analyzer.record_metrics(current_real_time, active_count, avg_stay,
                                    alpha, alpha_window, n_out, signage_state)
            analyzer.last_analysis_time = current_real_time

            time_since_save = (current_real_time - analyzer.last_save_time).total_seconds()
            if time_since_save >= SAVE_INTERVAL:
                analyzer.save_realtime_metrics()
                analyzer.save_vehicle_events()
                analyzer.last_save_time = current_real_time
                print(f"💾 中間保存: {len(analyzer.metrics_log)} records "
                      f"at {current_real_time.strftime('%H:%M:%S')} | "
                      f"α={alpha:.3f} | {signage_state}")

        # ── UI 表示 ───────────────────────────────────────
        if SHOW_VIDEO:
            cv2.polylines(frame, [ZONE_POLYGON], True, (0, 255, 0), 2)
            if PARKING_ZONE_POLYGON is not None:
                cv2.polylines(frame, [PARKING_ZONE_POLYGON], True, (255, 165, 0), 2)
            cv2.line(frame, LINE_EXIT_START, LINE_EXIT_END, (0, 0, 255), 3)

            # 右上パネル
            box_w, box_h = 400, 250
            tx = width - box_w
            cv2.rectangle(frame, (tx, 0), (width, box_h), (0, 0, 0), -1)

            state_color = SIGNAGE_COLORS.get(signage_state, (255, 255, 255))

            def draw_txt(txt, y, col=(255, 255, 255)):
                cv2.putText(frame, txt, (tx + 10, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.62, col, 2)

            draw_txt(f"Time:  {current_real_time.strftime('%H:%M:%S')}", 28)
            draw_txt(f"[ROTARY EFFICIENCY SYSTEM]", 56, (0, 255, 255))
            draw_txt(f"Vehicles (Nt): {active_count}", 84, (0, 255, 0))
            draw_txt(f"Avg Stay:      {avg_stay:.1f}s", 112, (255, 255, 0))
            # 高負荷セッション中は専用の測定値を表示
            if analyzer.high_load_active and analyzer.high_load_session_stays:
                hl_avg = sum(analyzer.high_load_session_stays) / len(analyzer.high_load_session_stays)
                draw_txt(f"HL Avg Stay:   {hl_avg:.1f}s [{len(analyzer.high_load_session_stays)}s]",
                         140, (0, 165, 255))
                draw_txt(f"Alpha(HL):     {alpha:.4f}", 168, (200, 200, 255))
            else:
                draw_txt(f"Alpha(win):    {alpha:.4f}", 140, (200, 200, 255))
                draw_txt(f"Exits(window): {n_out}", 168, (200, 200, 200))
            draw_txt(f"State: {signage_state}", 200, state_color)
            draw_txt(f"alpha_window:  {alpha_window:.4f}", 228, (150, 150, 150))

            cv2.imshow("Rotary Efficiency Analysis", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

    cap.release()
    if SHOW_VIDEO:
        cv2.destroyAllWindows()

    # ── 後処理 ────────────────────────────────────────────
    print("\n🔄 解析完了処理中...")
    for vid, v_data in vehicles.items():
        if not v_data['finalized']:
            analyzer.record_vehicle_exit(vid, v_data, analyzer.last_analysis_time or START_TIME, 'EndOfVideo')
    for vid, l_info in lost_vehicles.items():
        analyzer.record_vehicle_exit(vid, l_info['data'], l_info['lost_time'], 'Timeout')

    analyzer.save_realtime_metrics(force=True)
    analyzer.save_vehicle_events()
    analyzer.generate_summary()
    analyzer.plot_metrics()
    analyzer.generate_html_report()

    print(f"\n✅ 解析完了!")
    print(f"📁 出力フォルダ: {analyzer.output_dir.resolve()}")


if __name__ == "__main__":
    main()
