"""Diagnosis helpers for NVR recording failures."""
from __future__ import annotations


def diagnose_ffmpeg_exit(exit_code: int | None, stderr: str) -> str:
    text = (stderr or "").lower()
    if "401" in text or "unauthorized" in text:
        return "RTSP認証エラー"
    if "404" in text or "not found" in text:
        return "RTSP URLエラー"
    if "connection timed out" in text or "timed out" in text:
        return "RTSPタイムアウト"
    if "connection refused" in text or "no route to host" in text:
        return "RTSP接続不可"
    if "invalid data" in text or "moov atom not found" in text:
        return "映像データ異常"
    if "i/o error" in text or "input/output error" in text:
        return "I/Oエラー"
    if "no space left" in text or "not enough space" in text:
        return "容量不足"
    if exit_code not in (0, None):
        return f"FFmpeg異常終了 exit_code={exit_code}"
    return "原因未特定"
