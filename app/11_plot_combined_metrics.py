import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path
from datetime import datetime, timedelta
import matplotlib.dates as mdates
import numpy as np

# ================= 設定エリア =================
# 解析対象のCSVファイル（複数指定可能）下記は例として11日分を指定していますが、必要に応じて追加・削除してください。
CSV_FILES = [
    'analysis_output/realtime_metrics_2025-11-01.csv',
    'analysis_output/realtime_metrics_2025-11-02.csv',
    'analysis_output/realtime_metrics_2025-11-03.csv',
    'analysis_output/realtime_metrics_2025-11-04.csv',
    'analysis_output/realtime_metrics_2025-11-05.csv',
    'analysis_output/realtime_metrics_2025-11-06.csv',
    'analysis_output/realtime_metrics_2025-11-07.csv',
    'analysis_output/realtime_metrics_2025-11-08.csv',
    'analysis_output/realtime_metrics_2025-11-09.csv',
    'analysis_output/realtime_metrics_2025-11-10.csv',
    'analysis_output/realtime_metrics_2025-11-11.csv',
]
# 解析結果の出力ディレクトリ（実行環境に合わせて設定してください）
OUTPUT_DIR = 'analysis_output/plots'

# グラフ表示設定
ALPHA_DAILY = 0.3  # 各日のラインの透明度
LINEWIDTH_DAILY = 1.0  # 各日のライン幅
LINEWIDTH_AVERAGE = 3.0  # 平均トレンドのライン幅
# ==========================================================


def normalize_to_time_of_day(timestamp):
    """
    日付に関係なく、時刻のみを抽出して統一した日付（1900-01-01）に正規化

    Args:
        timestamp: datetime オブジェクト
    Returns:
        datetime オブジェクト（日付が1900-01-01に統一されたもの）
    """
    return datetime(1900, 1, 1, timestamp.hour, timestamp.minute, timestamp.second)


def load_all_metrics(csv_files):
    """
    複数のCSVファイルを読み込み、日付ごとに分類

    Args:
        csv_files: CSVファイルのパスのリスト
    Returns:
        dict: {日付文字列: DataFrame} の辞書
    """
    data_by_date = {}

    for csv_file in csv_files:
        csv_path = Path(csv_file)

        if not csv_path.exists():
            print(f"⚠️  ファイルが見つかりません: {csv_path}")
            continue

        try:
            df = pd.read_csv(csv_path)
            df['timestamp'] = pd.to_datetime(df['timestamp'])

            # 日付を抽出
            date_str = df['timestamp'].iloc[0].strftime('%Y-%m-%d')

            # 時刻のみに正規化（全データを同じ日付に統一）
            df['time_normalized'] = df['timestamp'].apply(normalize_to_time_of_day)

            data_by_date[date_str] = df
            print(f"✅ 読み込み成功: {date_str} ({len(df)} records)")

        except Exception as e:
            print(f"❌ 読み込みエラー ({csv_file}): {e}")

    return data_by_date


def calculate_average_trend(data_by_date, metric_column, use_median=False):
    """
    全日のデータから平均トレンドまたは中央値トレンドを計算

    Args:
        data_by_date: 日付ごとのDataFrameの辞書
        metric_column: 集計する列名 ('vehicle_count' or 'avg_stay_time')
        use_median: Trueの場合は中央値、Falseの場合は平均値を使用
    Returns:
        DataFrame: 時刻ごとの平均値または中央値
    """
    # すべてのデータを結合
    all_data = []

    for date_str, df in data_by_date.items():
        temp_df = df[['time_normalized', metric_column]].copy()
        all_data.append(temp_df)

    combined_df = pd.concat(all_data, ignore_index=True)

    # 時刻ごとにグループ化して平均または中央値を計算
    # 時刻を秒単位に丸める（1秒ごとの集計）
    combined_df['time_rounded'] = combined_df['time_normalized'].dt.floor('s')

    if use_median:
        trend = combined_df.groupby('time_rounded')[metric_column].median().reset_index()
        trend.columns = ['time_normalized', f'{metric_column}_trend']
    else:
        trend = combined_df.groupby('time_rounded')[metric_column].mean().reset_index()
        trend.columns = ['time_normalized', f'{metric_column}_trend']

    return trend


def plot_combined_metrics(data_by_date, output_dir='plots', use_median=False):
    """
    全日のデータを1つのグラフに統合して表示

    Args:
        data_by_date: 日付ごとのDataFrameの辞書
        output_dir: 出力ディレクトリ
        use_median: Trueの場合は中央値、Falseの場合は平均値を使用
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not data_by_date:
        print("❌ データがありません")
        return

    # 日本語フォント設定
    plt.rcParams['font.family'] = 'DejaVu Sans'
    plt.rcParams['axes.unicode_minus'] = False

    # 色のパレット（日数が多くても対応できるように）
    colors = plt.cm.tab20(np.linspace(0, 1, max(20, len(data_by_date))))

    # グラフ作成
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10))

    # 期間情報
    date_list = sorted(data_by_date.keys())
    period_str = f"{date_list[0]} to {date_list[-1]}"

    # ========== グラフ1: 滞在台数の推移 ==========
    for idx, (date_str, df) in enumerate(sorted(data_by_date.items())):
        ax1.plot(df['time_normalized'], df['vehicle_count'],
                linestyle='-', color=colors[idx], alpha=ALPHA_DAILY,
                linewidth=LINEWIDTH_DAILY, label=date_str)

    # 平均または中央値トレンドを計算して描画
    trend_label = 'Median Trend' if use_median else 'Average Trend'
    trend_count = calculate_average_trend(data_by_date, 'vehicle_count', use_median)
    ax1.plot(trend_count['time_normalized'], trend_count['vehicle_count_trend'],
            linestyle='-', color='darkblue', linewidth=LINEWIDTH_AVERAGE,
            label=trend_label, zorder=10)

    ax1.set_xlabel('Time of Day', fontsize=12)
    ax1.set_ylabel('Vehicle Count', fontsize=12)
    ax1.set_title(f'Combined Vehicle Count in Area ({period_str})',
                 fontsize=14, fontweight='bold')
    ax1.grid(True, alpha=0.3, linestyle='--')
    ax1.legend(loc='upper left', bbox_to_anchor=(1.02, 1), fontsize=8, ncol=1)

    # X軸の時刻表示フォーマット設定
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax1.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')

    # ========== グラフ2: 平均滞在時間の推移 ==========
    for idx, (date_str, df) in enumerate(sorted(data_by_date.items())):
        ax2.plot(df['time_normalized'], df['avg_stay_time'],
                linestyle='-', color=colors[idx], alpha=ALPHA_DAILY,
                linewidth=LINEWIDTH_DAILY, label=date_str)

    # 平均または中央値トレンドを計算して描画
    trend_stay = calculate_average_trend(data_by_date, 'avg_stay_time', use_median)
    ax2.plot(trend_stay['time_normalized'], trend_stay['avg_stay_time_trend'],
            linestyle='-', color='darkred', linewidth=LINEWIDTH_AVERAGE,
            label=trend_label, zorder=10)

    ax2.set_xlabel('Time of Day', fontsize=12)
    ax2.set_ylabel('Avg Stay Time (sec)', fontsize=12)
    ax2.set_title(f'Combined Average Stay Time in Area ({period_str})',
                 fontsize=14, fontweight='bold')
    ax2.grid(True, alpha=0.3, linestyle='--')
    ax2.legend(loc='upper left', bbox_to_anchor=(1.02, 1), fontsize=8, ncol=1)

    # X軸の時刻表示フォーマット設定
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax2.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')

    plt.tight_layout()

    # ファイル名に期間と統計手法を含めて保存
    stat_method = 'median' if use_median else 'mean'
    plot_filename = f'metrics_combined_{stat_method}_{date_list[0]}_to_{date_list[-1]}.png'
    plot_path = output_dir / plot_filename
    plt.savefig(plot_path, dpi=200, bbox_inches='tight')
    plt.close()

    print(f"\n📈 統合グラフ保存: {plot_path}")

    # 統計情報を表示
    print(f"\n📊 統合統計情報:")
    print(f"   解析期間: {period_str}")
    print(f"   解析日数: {len(data_by_date)} 日")

    all_counts = []
    all_stay_times = []

    for df in data_by_date.values():
        all_counts.extend(df['vehicle_count'].tolist())
        all_stay_times.extend(df['avg_stay_time'].tolist())

    print(f"   総記録数: {len(all_counts)} records")
    print(f"   平均滞在台数: {np.mean(all_counts):.2f} 台")
    print(f"   最大滞在台数: {np.max(all_counts)} 台")
    print(f"   最小滞在台数: {np.min(all_counts)} 台")
    print(f"   平均滞在時間: {np.mean(all_stay_times):.2f} 秒")
    print(f"   最長平均滞在時間: {np.max(all_stay_times):.2f} 秒")


def plot_combined_metrics_html(data_by_date, output_dir='plots', use_median=False):
    """
    Plotly を使ったインタラクティブ HTML レポートを生成する

    Args:
        data_by_date: 日付ごとのDataFrameの辞書
        output_dir: 出力ディレクトリ
        use_median: Trueの場合は中央値、Falseの場合は平均値を使用
    """
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("⚠️  plotly が未インストールです。pip install plotly を実行してください。")
        return

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not data_by_date:
        return

    date_list = sorted(data_by_date.keys())
    period_str = f"{date_list[0]} to {date_list[-1]}"
    trend_label = 'Median Trend' if use_median else 'Average Trend'

    # 日数に応じた色パレット生成
    n = len(date_list)
    palette = [
        f'hsl({int(360 * i / max(n, 1))}, 65%, 55%)'
        for i in range(n)
    ]

    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=[
            f'Combined Vehicle Count in Area ({period_str})',
            f'Combined Average Stay Time in Area ({period_str})',
        ],
        vertical_spacing=0.10,
    )

    # ── Panel 1: 滞在台数 ────────────────────────────────────
    for idx, date_str in enumerate(date_list):
        df = data_by_date[date_str]
        fig.add_trace(go.Scatter(
            x=df['time_normalized'],
            y=df['vehicle_count'],
            mode='lines',
            name=date_str,
            line=dict(color=palette[idx], width=1),
            opacity=ALPHA_DAILY + 0.1,
            legendgroup=date_str,
            showlegend=True,
            hovertemplate=(
                f'<b>{date_str}</b><br>'
                '<b>時刻:</b> %{x|%H:%M:%S}<br>'
                '<b>滞在台数:</b> %{y} 台<extra></extra>'
            ),
        ), row=1, col=1)

    trend_count = calculate_average_trend(data_by_date, 'vehicle_count', use_median)
    fig.add_trace(go.Scatter(
        x=trend_count['time_normalized'],
        y=trend_count['vehicle_count_trend'],
        mode='lines',
        name=trend_label,
        line=dict(color='darkblue', width=3),
        legendgroup='trend',
        showlegend=True,
        hovertemplate=(
            f'<b>{trend_label}</b><br>'
            '<b>時刻:</b> %{x|%H:%M:%S}<br>'
            '<b>台数:</b> %{y:.2f} 台<extra></extra>'
        ),
    ), row=1, col=1)

    # ── Panel 2: 平均滞在時間 ────────────────────────────────
    for idx, date_str in enumerate(date_list):
        df = data_by_date[date_str]
        fig.add_trace(go.Scatter(
            x=df['time_normalized'],
            y=df['avg_stay_time'],
            mode='lines',
            name=date_str,
            line=dict(color=palette[idx], width=1),
            opacity=ALPHA_DAILY + 0.1,
            legendgroup=date_str,
            showlegend=False,
            hovertemplate=(
                f'<b>{date_str}</b><br>'
                '<b>時刻:</b> %{x|%H:%M:%S}<br>'
                '<b>平均滞在時間:</b> %{y:.1f} 秒<extra></extra>'
            ),
        ), row=2, col=1)

    trend_stay = calculate_average_trend(data_by_date, 'avg_stay_time', use_median)
    fig.add_trace(go.Scatter(
        x=trend_stay['time_normalized'],
        y=trend_stay['avg_stay_time_trend'],
        mode='lines',
        name=trend_label,
        line=dict(color='darkred', width=3),
        legendgroup='trend',
        showlegend=False,
        hovertemplate=(
            f'<b>{trend_label}</b><br>'
            '<b>時刻:</b> %{x|%H:%M:%S}<br>'
            '<b>平均滞在時間:</b> %{y:.2f} 秒<extra></extra>'
        ),
    ), row=2, col=1)

    # ── レイアウト ───────────────────────────────────────────
    fig.update_layout(
        height=800,
        title_text=f'Combined Rotary Metrics — {trend_label} ({period_str})',
        title_font_size=18,
        plot_bgcolor='white',
        paper_bgcolor='#f8f9fa',
        font=dict(family='Arial, sans-serif', size=12),
        hovermode='closest',
        legend=dict(
            title='日付',
            font=dict(size=10),
            bgcolor='rgba(255,255,255,0.85)',
            bordercolor='rgba(0,0,0,0.2)',
            borderwidth=1,
        ),
        margin=dict(l=80, r=200, t=80, b=60),
    )

    for r in range(1, 3):
        fig.update_xaxes(
            showgrid=True, gridcolor='rgba(0,0,0,0.08)',
            tickformat='%H:%M',
            title_text='Time of Day',
            row=r, col=1,
        )
        fig.update_yaxes(
            showgrid=True, gridcolor='rgba(0,0,0,0.08)',
            row=r, col=1,
        )

    fig.update_yaxes(title_text='Vehicle Count', row=1, col=1)
    fig.update_yaxes(title_text='Avg Stay Time (sec)', row=2, col=1)

    stat_method = 'median' if use_median else 'mean'
    html_filename = f'metrics_combined_{stat_method}_{date_list[0]}_to_{date_list[-1]}.html'
    html_path = output_dir / html_filename
    fig.write_html(str(html_path), include_plotlyjs='cdn')
    print(f"🌐 HTMLレポート保存: {html_path}")


def generate_summary_stats(data_by_date, output_dir='plots'):
    """
    統計サマリーをテキストファイルとして保存

    Args:
        data_by_date: 日付ごとのDataFrameの辞書
        output_dir: 出力ディレクトリ
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    date_list = sorted(data_by_date.keys())
    period_str = f"{date_list[0]}_to_{date_list[-1]}"

    summary_path = output_dir / f'combined_summary_{period_str}.txt'

    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write("=" * 70 + "\n")
        f.write("統合リアルタイム解析サマリー\n")
        f.write("=" * 70 + "\n\n")

        f.write(f"解析期間: {date_list[0]} to {date_list[-1]}\n")
        f.write(f"解析日数: {len(data_by_date)} 日\n\n")

        f.write("-" * 70 + "\n")
        f.write("日別統計:\n")
        f.write("-" * 70 + "\n\n")

        for date_str, df in sorted(data_by_date.items()):
            f.write(f"📅 {date_str}:\n")
            f.write(f"   記録数: {len(df)} records\n")
            f.write(f"   平均滞在台数: {df['vehicle_count'].mean():.2f} 台\n")
            f.write(f"   最大滞在台数: {df['vehicle_count'].max()} 台\n")
            f.write(f"   平均滞在時間: {df['avg_stay_time'].mean():.2f} 秒\n\n")

        # 全体統計
        all_counts = []
        all_stay_times = []

        for df in data_by_date.values():
            all_counts.extend(df['vehicle_count'].tolist())
            all_stay_times.extend(df['avg_stay_time'].tolist())

        f.write("-" * 70 + "\n")
        f.write("全体統計:\n")
        f.write("-" * 70 + "\n\n")
        f.write(f"総記録数: {len(all_counts)} records\n")
        f.write(f"平均滞在台数: {np.mean(all_counts):.2f} 台\n")
        f.write(f"最大滞在台数: {np.max(all_counts)} 台\n")
        f.write(f"最小滞在台数: {np.min(all_counts)} 台\n")
        f.write(f"平均滞在時間: {np.mean(all_stay_times):.2f} 秒\n")
        f.write(f"最長平均滞在時間: {np.max(all_stay_times):.2f} 秒\n")

    print(f"📄 サマリー保存: {summary_path}")


def main():
    """メイン処理"""
    print("=" * 70)
    print("統合リアルタイムメトリクス グラフ生成ツール")
    print("=" * 70)
    print()

    # データ読み込み
    print("📂 データ読み込み中...\n")
    data_by_date = load_all_metrics(CSV_FILES)

    if not data_by_date:
        print("\n❌ 読み込めるデータがありませんでした")
        return

    print(f"\n✅ {len(data_by_date)} 日分のデータを読み込みました\n")

    # 統合グラフ生成（平均値版）
    print("📈 統合グラフを生成中（平均値）...\n")
    plot_combined_metrics(data_by_date, OUTPUT_DIR, use_median=False)
    plot_combined_metrics_html(data_by_date, OUTPUT_DIR, use_median=False)

    # 統合グラフ生成（中央値版）
    print("\n📈 統合グラフを生成中（中央値）...\n")
    plot_combined_metrics(data_by_date, OUTPUT_DIR, use_median=True)
    plot_combined_metrics_html(data_by_date, OUTPUT_DIR, use_median=True)

    # サマリー生成
    print("\n📄 統計サマリーを生成中...\n")
    generate_summary_stats(data_by_date, OUTPUT_DIR)

    print("\n" + "=" * 70)
    print(f"✅ 処理完了: 平均値版と中央値版の両方を生成しました")
    print(f"📁 出力フォルダ: {Path(OUTPUT_DIR).resolve()}")
    print("=" * 70)


if __name__ == "__main__":
    main()
