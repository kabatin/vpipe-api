"""Thin wrappers around ffprobe / ffmpeg and Pillow."""

from __future__ import annotations

import io
import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

# The only Pillow decoders that ever see untrusted bytes.
IMAGE_DECODERS = ("PNG", "JPEG", "WEBP")
# MPO = multi-picture JPEG written by many cameras/phones; its first frame is a plain JPEG.
# It has no opener of its own (the JPEG decoder returns it), so it is mapped but not decoded.
IMAGE_MEDIA_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",
    "WEBP": "image/webp",
}
MAX_IMAGE_PIXELS = 50_000_000
FFMPEG_TIMEOUT_S = 600


class MediaError(RuntimeError):
    """ffmpeg/ffprobe failed or an input is not usable."""


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    frames: int
    fps: float
    duration_sec: float
    has_audio: bool


def normalize_image(data: bytes, media_type: str) -> bytes:
    """Validate an uploaded image and re-encode it as an upright RGB PNG."""
    try:
        with Image.open(io.BytesIO(data), formats=list(IMAGE_DECODERS)) as img:
            actual = IMAGE_MEDIA_TYPES.get(img.format or "")
            if actual is None:
                raise MediaError(f"unsupported image format {img.format!r}")
            if actual != media_type:
                raise MediaError(f"media_type {media_type!r} does not match the data ({actual})")
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise MediaError(f"image too large ({img.width}x{img.height})")
            upright = ImageOps.exif_transpose(img).convert("RGB")
    except MediaError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise MediaError(f"cannot decode image ({type(exc).__name__})") from exc
    except Exception as exc:  # Pillow plugins raise ValueError/OSError/SyntaxError/...
        raise MediaError(f"cannot decode image ({type(exc).__name__})") from exc
    out = io.BytesIO()
    upright.save(out, format="PNG")
    return out.getvalue()


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"{Path(cmd[0]).name} failed to run: {exc}") from exc
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise MediaError(f"{Path(cmd[0]).name} exited {result.returncode}: {' | '.join(tail)}")
    return result


def probe_video(path: Path, ffprobe: str = "ffprobe") -> VideoInfo:
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,nb_frames,r_frame_rate:format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError(f"{path.name} has no video stream")
    fps = float(Fraction(video.get("r_frame_rate", "0/1")))
    duration = float(data.get("format", {}).get("duration", 0.0))
    frames_raw = video.get("nb_frames")
    frames = int(frames_raw) if frames_raw not in (None, "N/A") else round(duration * fps)
    return VideoInfo(
        width=int(video["width"]),
        height=int(video["height"]),
        frames=frames,
        fps=fps,
        duration_sec=duration,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def finalize_video(
    src: Path,
    dst: Path,
    *,
    width: int,
    height: int,
    comment: str,
    ffmpeg: str = "ffmpeg",
    source_size: tuple[int, int] | None = None,
) -> None:
    """Scale (cover + centre crop, lanczos) to exactly ``width``x``height`` and drop audio.

    The ``comment`` metadata makes every output byte-unique, so a consumer that
    de-duplicates by checksum never mistakes a new clip for an old one.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part.mp4")
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(src), "-an"]
    if source_size == (width, height):
        cmd += ["-c:v", "copy"]
    else:
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height},setsar=1"
        )
        cmd += [
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
        ]
    cmd += ["-metadata", f"comment={comment}", "-movflags", "+faststart", str(tmp)]
    _run(cmd)
    tmp.replace(dst)
