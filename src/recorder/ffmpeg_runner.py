"""Thin subprocess wrapper around FFmpeg/FFprobe."""
from __future__ import annotations

import subprocess
from pathlib import Path


class FFmpegRunner:
    def __init__(self, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

    def build_record_command(self, rtsp_url: str, output_path: str | Path) -> list[str]:
        return [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-i",
            rtsp_url,
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(output_path),
        ]

    def start_recording(self, rtsp_url: str, output_path: str | Path) -> subprocess.Popen[str]:
        cmd = self.build_record_command(rtsp_url, output_path)
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def stop_recording(self, process: subprocess.Popen[str], timeout: int = 10) -> tuple[int | None, str]:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            return process.returncode, stderr
        try:
            if process.stdin:
                process.stdin.write("q\n")
                process.stdin.flush()
            _, stderr = process.communicate(timeout=timeout)
            return process.returncode, stderr or ""
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                _, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                _, stderr = process.communicate(timeout=5)
            return process.returncode, stderr or ""

    def test_rtsp(self, rtsp_url: str, timeout: int = 10) -> tuple[bool, str]:
        cmd = [
            self.ffprobe_path,
            "-v",
            "error",
            "-rtsp_transport",
            "tcp",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1",
            rtsp_url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
            return result.returncode == 0, (result.stderr or result.stdout or "OK").strip()
        except FileNotFoundError:
            return False, f"FFprobeが見つかりません: {self.ffprobe_path}"
        except subprocess.TimeoutExpired:
            return False, "接続テストがタイムアウトしました"
