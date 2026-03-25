from __future__ import annotations

from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd


def _normalize_time(ts: pd.Timestamp) -> pd.Timestamp:
    # 【脇村モデル】多日集計可視化
    return pd.Timestamp(year=1900, month=1, day=1, hour=ts.hour, minute=ts.minute, second=ts.second)


def build_multi_day_trend(metrics_files: list[Path], metric_col: str) -> pd.DataFrame:
    frames = []
    for path in metrics_files:
        if not path.exists():
            continue
        df = pd.read_csv(path)
        if "timestamp" not in df.columns or metric_col not in df.columns:
            continue
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df["time_normalized"] = df["timestamp"].apply(_normalize_time)
        frames.append(df[["time_normalized", metric_col]])

    if not frames:
        return pd.DataFrame(columns=["time_normalized", "avg", "median"])

    merged = pd.concat(frames, ignore_index=True)
    merged["time_rounded"] = merged["time_normalized"].dt.floor("1min")

    grouped = merged.groupby("time_rounded")[metric_col]
    out = grouped.mean().rename("avg").to_frame()
    # 【脇村モデル】平均トレンド
    out["median"] = grouped.median()
    # 【脇村モデル】中央値トレンド
    return out.reset_index().rename(columns={"time_rounded": "time_normalized"})


def save_multi_day_trend_plot(metrics_files: list[Path], metric_col: str, output_png: Path) -> None:
    trend = build_multi_day_trend(metrics_files, metric_col)
    if trend.empty:
        return
    output_png.parent.mkdir(parents=True, exist_ok=True)

    plt.figure(figsize=(12, 4.5))
    plt.plot(trend["time_normalized"], trend["avg"], label="Average", linewidth=2)
    plt.plot(trend["time_normalized"], trend["median"], label="Median", linewidth=2)
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=1))
    plt.xticks(rotation=45)
    plt.grid(alpha=0.3)
    plt.title(f"Multi-day trend: {metric_col}")
    plt.tight_layout()
    plt.legend()
    plt.savefig(output_png, dpi=150)
    plt.close()
