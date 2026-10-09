"""Thin wrappers around ffprobe / ffmpeg and Pillow."""

from __future__ import annotations

import io
import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

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
# Uploaded videos are untrusted: only the MP4 demuxer, these decoders, and local files.
SOURCE_VIDEO_CODECS = ("h264", "hevc")
SOURCE_AUDIO_CODECS = ("aac",)
MIN_SOURCE_SIDE = 16
MAX_SOURCE_SIDE = 4096
MAX_SOURCE_RATE = 60
# a coarse header check only; the workflow enforces its own limit on the counted frames
MAX_SOURCE_DURATION_HINT = 40
SOURCE_PIX_FMTS = ("yuv420p", "yuvj420p", "yuv422p", "yuvj422p", "yuv444p", "yuvj444p")
HDR_TRANSFERS = ("arib-std-b67", "smpte2084")
_UNTRUSTED_MP4 = [
    "-f",
    "mov",
    "-protocol_whitelist",
    "file",
    "-codec_whitelist",
    ",".join(SOURCE_VIDEO_CODECS + SOURCE_AUDIO_CODECS),
]
# vpipe's intermediates are full-range BT.709 (h3_graph, flashvsr_graph). That is stated to
# the scaler rather than read from the tags, which a remux can drop.
_FROM_FULL_BT709 = ":in_color_matrix=bt709:in_range=pc:out_color_matrix=bt709:out_range=tv"
# setparams, not -color_* output flags: the encoder takes the tags from the frames
_AS_LIMITED_BT709 = (
    "setsar=1,format=yuv420p,"
    "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709"
)
_H264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p"]


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


@dataclass(frozen=True)
class SourceInfo:
    """An uploaded clip, as far as ``probe_source`` trusts it."""

    width: int
    height: int
    frames: int
    rate: Fraction
    duration_sec: float
    audio_codec: str | None


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


def encode_timeout(frames: int, width: int, height: int) -> float:
    """At least FFMPEG_TIMEOUT_S; long, large encodes get ~4x what an M5 needs (0.17 s for a
    4096x2304 frame with lanczos + x264 medium)."""
    return max(FFMPEG_TIMEOUT_S, frames * width * height / 5e6)


def _run(cmd: list[str], timeout: float = FFMPEG_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"{Path(cmd[0]).name} failed to run: {exc}") from exc
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise MediaError(f"{Path(cmd[0]).name} exited {result.returncode}: {' | '.join(tail)}")
    return result


def probe_video(path: Path, ffprobe: str = "ffprobe") -> VideoInfo:
    """Frames come from the container's count, else from counting packets.

    Matroska stores no frame count, and its duration covers the (longer) audio track,
    so duration x fps overcounts; counting packets only demuxes, it does not decode.
    """
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-count_packets",
            "-show_entries",
            "stream=codec_type,width,height,nb_frames,nb_read_packets,r_frame_rate:format=duration",
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
    counts = (video.get("nb_frames"), video.get("nb_read_packets"))
    frames = next((int(n) for n in counts if n not in (None, "N/A")), round(duration * fps))
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
    fps: int | Fraction,
    comment: str,
    ffmpeg: str = "ffmpeg",
) -> None:
    """Scale (cover + centre crop, lanczos) to exactly ``width``x``height`` and drop audio.

    Always re-encodes, even at the same size: the source is vpipe's lossless intermediate
    (FFV1, full-range BT.709 -- see ``h3_graph``), which players cannot open. That colour
    is stated to the scaler rather than read from the tags, which a remux can drop; the
    output is limited-range BT.709 H.264 and tagged as such. Likewise the frame rate is
    ``fps``, whatever the source's timestamps say.

    The ``comment`` metadata makes every output byte-unique, so a consumer that
    de-duplicates by checksum never mistakes a new clip for an old one.
    """
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos"
        f"{_FROM_FULL_BT709},crop={width}:{height},{_AS_LIMITED_BT709}"
    )
    _encode([*_vpipe_input(src, fps), "-an", "-vf", vf], dst, comment, ffmpeg)


def finalize_cropped(
    src: Path,
    dst: Path,
    *,
    width: int,
    height: int,
    fps: int | Fraction,
    comment: str,
    ffmpeg: str = "ffmpeg",
) -> None:
    """``finalize_video`` without the scaling: centre-crop to ``width``x``height`` (no
    larger than the source) at the source's own pixels.

    The crop comes first, on the 4:4:4 intermediate, so an odd offset stays exact (a crop
    after the conversion to 4:2:0 rounds it down to even; ``exact`` keeps it so even then).
    """
    crop = f"crop={width}:{height}:exact=1"
    vf = f"{crop},scale=flags=lanczos{_FROM_FULL_BT709},{_AS_LIMITED_BT709}"
    _encode([*_vpipe_input(src, fps), "-an", "-vf", vf], dst, comment, ffmpeg)


def probe_source(path: Path, ffprobe: str = "ffprobe") -> SourceInfo:
    """Read an uploaded MP4 with the untrusted-input limits; the error says what to fix.

    The size is the upright one (a phone's rotation tag swaps it; ffmpeg rotates the frames
    when it decodes them). Frames are the packets the decoder keeps: a clip trimmed at a
    non-keyframe carries packets an edit list discards, and its header counts them too.
    """
    entries = (
        "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,"
        "pix_fmt,color_transfer,color_primaries:format=duration"
    )
    query = ["-show_entries", f"{entries}:stream_side_data=rotation"]
    data = json.loads(_probe(ffprobe, path, query, "json"))
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("source_video has no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    if _quarter_turn(video):
        width, height = height, width
    duration = float(data.get("format", {}).get("duration", 0))
    rate = _check_source(video, width, height, duration)
    query = ["-select_streams", "v:0", "-show_entries", "packet=flags"]
    flags = _probe(ffprobe, path, query, "csv=p=0")
    frames = sum(1 for line in flags.splitlines() if line.strip() and "D" not in line)
    if frames <= 0:
        raise MediaError("source_video has no frames")
    return SourceInfo(
        width=width,
        height=height,
        frames=frames,
        rate=rate,
        duration_sec=float(frames / rate),
        audio_codec=audio.get("codec_name") if audio is not None else None,
    )


def _check_source(video: dict[str, Any], width: int, height: int, duration: float) -> Fraction:
    """Cheap checks on the header, before the packets are listed; returns the frame rate."""
    rate = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    if min(width, height) < MIN_SOURCE_SIDE or max(width, height) > MAX_SOURCE_SIDE:
        raise MediaError(f"source_video must be {MIN_SOURCE_SIDE}..{MAX_SOURCE_SIDE} pixels a side")
    if rate is None or rate > MAX_SOURCE_RATE:
        raise MediaError(f"source_video needs a frame rate of at most {MAX_SOURCE_RATE} fps")
    if duration > 2 * MAX_SOURCE_DURATION_HINT:
        raise MediaError("source_video is far too long")
    if video.get("color_transfer") in HDR_TRANSFERS or video.get("color_primaries") == "bt2020":
        raise MediaError("source_video is HDR (HLG/PQ or BT.2020); send SDR BT.709")
    if video.get("pix_fmt") not in SOURCE_PIX_FMTS:
        raise MediaError("source_video must be 8-bit video (yuv420p, yuv422p or yuv444p)")
    return rate


def _probe(ffprobe: str, path: Path, query: list[str], fmt: str) -> str:
    try:
        return _run([ffprobe, "-v", "error", *_UNTRUSTED_MP4, *query, "-of", fmt, str(path)]).stdout
    except MediaError as exc:
        if "not on whitelist" in str(exc):
            raise MediaError(
                "source_video: only H.264/HEVC video and AAC audio tracks are accepted "
                "(no other codecs, no subtitles)"
            ) from exc
        raise MediaError("source_video is not a readable MP4") from exc


def _quarter_turn(stream: dict[str, Any]) -> bool:
    for item in stream.get("side_data_list") or []:
        rotation = item.get("rotation") if isinstance(item, dict) else None
        if isinstance(rotation, int | float) and round(rotation) % 180 == 90:
            return True
    return False


def pad_and_crop_source(
    src: Path,
    dst: Path,
    *,
    crop: tuple[int, int],
    size: tuple[int, int] | None,
    front: int,
    back: int,
    frames_hint: int = 0,
    ffmpeg: str = "ffmpeg",
) -> None:
    """Centre-crop to ``crop`` (w, h), stretch to ``size`` if given (a source larger than
    vpipe's processing size), and clone the first/last frame ``front``/``back`` times.

    Written losslessly (FFV1 in Matroska), video only and upright: this is what vpipe
    reads, so vpipe never parses the upload itself.
    """
    width, height = crop
    resize = f"scale={size[0]}:{size[1]}:flags=lanczos," if size is not None else ""
    vf = (
        f"crop={width}:{height},{resize}"
        f"tpad=start={front}:start_mode=clone:stop={back}:stop_mode=clone"
    )
    cmd = [ffmpeg, "-y", "-v", "error", *_UNTRUSTED_MP4, "-i", str(src), "-map", "0:v:0"]
    cmd += ["-vf", vf, "-fps_mode", "passthrough", "-c:v", "ffv1", "-an", str(dst)]
    out_w, out_h = size or crop
    _run(cmd, encode_timeout(frames_hint, out_w, out_h))


def finalize_upscale(
    src: Path,
    source: Path,
    dst: Path,
    *,
    width: int,
    height: int,
    frames: int,
    rate: Fraction,
    audio: bool,
    comment: str,
    ffmpeg: str = "ffmpeg",
) -> None:
    """The first ``frames`` frames of vpipe's output at ``rate``, stretched to the size.

    vpipe works on the 128-pixel grid, so the frames are a slightly stretched copy of a
    crop that already has the output's shape; stretching back restores it. ``audio`` puts
    the source's audio track back unchanged (``-r`` retimes the video input only).
    """
    vf = f"scale={width}:{height}:flags=lanczos{_FROM_FULL_BT709},{_AS_LIMITED_BT709}"
    sound = (
        [*_UNTRUSTED_MP4, "-i", str(source), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy"]
        if audio
        else ["-an"]
    )
    args = [*_vpipe_input(src, rate), *sound, "-vf", vf, "-frames:v", str(frames)]
    _encode(args, dst, comment, ffmpeg, encode_timeout(frames, width, height))


def _vpipe_input(src: Path, rate: int | Fraction) -> list[str]:
    # -r before -i: frame n is at n/rate. vpipe's Matroska has millisecond timestamps and no
    # frame rate, and with large FFV1 frames ffmpeg guesses one from a couple of them
    # (e.g. 24000/1001 at 1344x768).
    return ["-r", str(rate), "-i", str(src)]


def _encode(
    args: list[str], dst: Path, comment: str, ffmpeg: str, timeout: float = FFMPEG_TIMEOUT_S
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part.mp4")
    # passthrough: one frame out per frame in, never a dropped or duplicated one
    cmd = [ffmpeg, "-y", "-v", "error", *args, "-fps_mode", "passthrough", *_H264]
    cmd += ["-metadata", f"comment={comment}", "-movflags", "+faststart", str(tmp)]
    _run(cmd, timeout)
    tmp.replace(dst)


def _rate(value: str | None) -> Fraction | None:
    try:
        rate = Fraction(value or "")
    except (ValueError, ZeroDivisionError):
        return None
    return rate if 0 < rate <= 240 else None
