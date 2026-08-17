from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


class MediaValidationError(ValueError):
    pass


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None
    format_name: str
    timing_candidates: list[float]

    def as_dict(self) -> dict[str, object]:
        return {
            "duration_seconds": round(self.duration_seconds, 3),
            "width": self.width,
            "height": self.height,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "format_name": self.format_name,
            "timing_candidates": self.timing_candidates,
        }


async def _run(*args: str) -> tuple[int, str, str]:
    import asyncio

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def probe_video(path: Path, max_seconds: float) -> MediaProbe:
    code, stdout, stderr = await _run(
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    )
    if code != 0:
        raise MediaValidationError(f"FFprobe could not read this video: {stderr[-300:]}")
    payload = json.loads(stdout)
    video_stream = next((s for s in payload.get("streams", []) if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in payload.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not video_stream:
        raise MediaValidationError("The file contains no video stream")
    duration = float(payload.get("format", {}).get("duration") or video_stream.get("duration") or 0)
    if duration <= 0:
        raise MediaValidationError("Video duration could not be determined")
    if duration > max_seconds + 0.05:
        raise MediaValidationError(f"Campaign is {duration:.2f}s; the current limit is {max_seconds:.0f}s")

    scene_code, _, scene_stderr = await _run(
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(path),
        "-vf",
        "select=gt(scene\\,0.28),showinfo",
        "-an",
        "-f",
        "null",
        "-",
    )
    candidates: list[float] = []
    if scene_code in (0, 1):
        candidates = sorted({round(float(value), 3) for value in re.findall(r"pts_time:([0-9.]+)", scene_stderr) if 0 < float(value) < duration})

    return MediaProbe(
        duration_seconds=duration,
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        video_codec=str(video_stream.get("codec_name") or "unknown"),
        audio_codec=str(audio_stream.get("codec_name")) if audio_stream else None,
        format_name=str(payload.get("format", {}).get("format_name") or "unknown"),
        timing_candidates=candidates[:20],
    )


async def extract_jpeg_frame(path: Path, timestamp_seconds: float) -> bytes:
    import asyncio

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0, timestamp_seconds):.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        "scale='min(1600,iw)':-2",
        "-q:v",
        "3",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode or not stdout:
        raise MediaValidationError(f"FFmpeg could not extract a review frame: {stderr.decode('utf-8', 'replace')[-300:]}")
    return stdout


async def normalize_playback_mp4(source: Path, destination: Path) -> None:
    code, _, stderr = await _run(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        "scale='min(1920,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(destination),
    )
    if code != 0 or not destination.exists() or destination.stat().st_size == 0:
        raise MediaValidationError(f"FFmpeg could not create the H.264 playback copy: {stderr[-300:]}")
