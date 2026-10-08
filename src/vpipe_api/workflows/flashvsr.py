"""Workflow ``flashvsr-upscale``: FlashVSR v1.1 super-resolution of an uploaded clip.

The clip comes back at exactly the requested size with its own frame count, frame rate
and audio track (if any). It is centre-cropped to the output's shape, processed on
FlashVSR's 128-pixel grid (at most 1920x1152, vpipe's own pipeline size), then resized
to the output. FlashVSR returns whole 21-frame groups and never the last frames of a
clip, so the last frame is cloned before the run and everything past the source is cut.
"""

from __future__ import annotations

import base64
import math
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vpipe_api.media import (
    MediaError,
    VideoInfo,
    finalize_upscale,
    pad_and_crop_source,
    probe_source,
    probe_video,
)
from vpipe_api.workflows.base import (
    InvalidParamsError,
    MediaTools,
    PreparedRun,
    ProgressMapper,
    RequiredModel,
    Workflow,
    WorkflowFailedError,
    WorkflowOutput,
)
from vpipe_api.workflows.flashvsr_graph import (
    GRID,
    GROUP_OVERLAP,
    GROUP_STEP,
    FlashVsrGraphInputs,
    build_flashvsr_spec,
)
from vpipe_api.workflows.inputs import OutputSize, aspect_ok, decode_base64

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_SECONDS = 40
MAX_PIXELS = 1920 * 1152
MAX_STRETCH = math.log(1152 / 1080) + 1e-9  # what vpipe's own pipeline does to 1920x1080
DEFAULT_LONG_SIDE = 1920
MODEL_KEY = "JunhaoZhuang/FlashVSR-v1.1"
# Measured on an M5 (10-core GPU, 32 GB) at 1920x1152, whole jobs: 3 groups = 304-312 s,
# 4 = 413 s, 10 = 1017 s (vpipe alone: 2 groups = 210 s). Nearly all of it is per group:
# vpipe reloads the DiT for each one to make room for the VAE decode, which does not shrink
# with the picture, so GROUP_FLOOR_SECONDS is kept at any size and the rest is assumed to
# scale with the processing pixels (not measured below 1920x1152).
SECONDS_PER_GROUP = 101
GROUP_FLOOR_SECONDS = 30


def processing_size(width: int, height: int) -> tuple[int, int]:
    """The output size rounded up to the grid. Above MAX_PIXELS: the grid size with the most
    pixels within it that is no more stretched than 1920x1080 -> 1920x1152 (vpipe's own)."""
    up = (_grid_ceil(width), _grid_ceil(height))
    if up[0] * up[1] <= MAX_PIXELS:
        return up
    aspect = width / height
    sizes = [
        (w, h)
        for h in range(GRID, MAX_PIXELS // GRID + 1, GRID)
        for w in range(GRID, MAX_PIXELS // h + 1, GRID)
    ]
    fitting = [size for size in sizes if _stretch(size, aspect) <= MAX_STRETCH] or sizes
    return max(fitting, key=lambda size: (size[0] * size[1], -_stretch(size, aspect)))


def _stretch(size: tuple[int, int], aspect: float) -> float:
    return abs(math.log(size[0] / size[1] / aspect))


def cover_crop(width: int, height: int, out_width: int, out_height: int) -> tuple[int, int]:
    """The largest centred crop of the source with the output's aspect ratio (even sizes)."""
    if width * out_height > height * out_width:  # source is wider
        return min(_even_floor(height * out_width / out_height), width), _even_floor(height)
    return _even_floor(width), min(_even_floor(width * out_height / out_width), height)


@dataclass(frozen=True)
class Padding:
    back: int  # clones of the last frame
    groups: int


def padding(frames: int) -> Padding:
    """Clones of the last frame so that whole groups return every source frame.

    Measured with frame numbers burned into a clip: output frame n is source frame n, and
    a clip's last GROUP_OVERLAP frames (and any partial group) never come back, so nothing
    is needed in front.
    """
    groups = max(1, math.ceil(frames / GROUP_STEP))
    total = groups * GROUP_STEP + GROUP_OVERLAP
    return Padding(back=total - frames, groups=groups)


def _grid_ceil(value: float) -> int:
    return max(GRID, math.ceil(value / GRID) * GRID)


def _grid_floor(value: float) -> int:
    return max(GRID, math.floor(value / GRID) * GRID)


def _even(value: float) -> int:
    return max(2, round(value / 2) * 2)


def _even_floor(value: float) -> int:
    return max(2, math.floor(value / 2) * 2)


class VideoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: str = Field(description="base64-encoded MP4", json_schema_extra={"format": "base64"})
    media_type: Literal["video/mp4"]

    @field_validator("data")
    @classmethod
    def _decodable(cls, value: str) -> str:
        decode_base64(value, MAX_SOURCE_BYTES, "video")
        return value

    def decoded(self) -> bytes:
        return decode_base64(self.data, MAX_SOURCE_BYTES, "video")


class FlashVsrParams(BaseModel):
    """POST body for ``flashvsr-upscale``."""

    model_config = ConfigDict(extra="forbid")

    source_video: VideoInput = Field(
        description=f"H.264/HEVC MP4 (AAC audio is kept), up to {MAX_SOURCE_SECONDS} s "
        f"and {MAX_SOURCE_BYTES >> 20} MB"
    )
    output: OutputSize | None = Field(
        default=None,
        description="final size; default: the source's shape with a 1920-pixel long side",
    )


class FlashVsrOptions(BaseModel):
    """``[workflows."flashvsr-upscale"]`` in the config file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_key: str = MODEL_KEY


_DEFAULT_MODEL = RequiredModel(
    key=MODEL_KEY,
    path=MODEL_KEY,
    files=(
        "diffusion_pytorch_model_streaming_dmd.safetensors",
        "LQ_proj_in.ckpt",
        "posi_prompt.pth",
        "Wan2.1_VAE.pth",
    ),
    hf_fetch=True,
    disk_gb_needed=8,
)


class FlashVsrWorkflow(Workflow):
    id = "flashvsr-upscale"
    description = (
        "FlashVSR v1.1 super-resolution of an uploaded MP4 to the requested size; "
        "same frame count, frame rate and audio"
    )
    output_media_type = "video/mp4"
    params_model = FlashVsrParams

    def __init__(self, media: MediaTools, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(media)
        self.options = FlashVsrOptions.model_validate(dict(options or {}))

    @property
    def required_models(self) -> tuple[RequiredModel, ...]:
        if self.options.model_key == MODEL_KEY:
            return (_DEFAULT_MODEL,)
        return (RequiredModel(key=self.options.model_key, path=self.options.model_key),)

    def store_inputs(self, params: BaseModel, job_dir: Path) -> dict[str, Any]:
        p = cast(FlashVsrParams, params)
        inputs = job_dir / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        path = inputs / "source.mp4"
        path.write_bytes(p.source_video.decoded())
        try:
            info = probe_source(path, self.media.ffprobe)
        except MediaError as exc:
            raise InvalidParamsError(str(exc)) from exc
        if info.frames / info.rate > MAX_SOURCE_SECONDS:
            raise InvalidParamsError(f"source_video is longer than {MAX_SOURCE_SECONDS} s")
        output = p.output or _default_output(info.width, info.height)
        return {
            "source_video": str(path.relative_to(job_dir)),
            "source": {
                "width": info.width,
                "height": info.height,
                "frames": info.frames,
                "rate": str(info.rate),
                "audio_codec": info.audio_codec,
            },
            "output": output.model_dump(),
        }

    def estimate_seconds(self, params: Mapping[str, Any]) -> float:
        out = params["output"]
        width, height = processing_size(out["width"], out["height"])
        groups = padding(params["source"]["frames"]).groups
        scaled = (SECONDS_PER_GROUP - GROUP_FLOOR_SECONDS) * width * height / MAX_PIXELS
        return 5 + groups * (GROUP_FLOOR_SECONDS + scaled)

    def prepare(self, job_id: str, params: Mapping[str, Any], job_dir: Path) -> PreparedRun:
        source, out = params["source"], params["output"]
        crop = cover_crop(source["width"], source["height"], out["width"], out["height"])
        pad = padding(source["frames"])
        width, height = processing_size(out["width"], out["height"])
        padded = (job_dir / "inputs" / "padded.mkv").resolve()  # deleted with the inputs
        expected = source["frames"] + pad.back
        try:
            pad_and_crop_source(
                job_dir / params["source_video"],
                padded,
                crop=crop,
                size=(width, height) if crop[0] * crop[1] > width * height else None,
                front=0,
                back=pad.back,
                frames_hint=expected,
                ffmpeg=self.media.ffmpeg,
            )
            got = probe_video(padded, self.media.ffprobe).frames
        except MediaError as exc:
            raise WorkflowFailedError("preprocess_failed", str(exc), retryable=False) from exc
        if got != expected:  # vpipe would drop the short last group and come back short
            raise WorkflowFailedError(
                "preprocess_failed",
                f"source decoded to {got - pad.back} frames, not {source['frames']}",
                retryable=False,
            )
        raw = (job_dir / "raw.mkv").resolve()
        spec = build_flashvsr_spec(
            f"vpipe-api-{job_id}",
            self.options.model_key,
            FlashVsrGraphInputs(
                source=padded,
                width=width,
                height=height,
                rate=Fraction(source["rate"]),
                output=raw,
            ),
        )
        return PreparedRun(spec=spec, raw_output=raw)

    def finalize(
        self, job_id: str, params: Mapping[str, Any], job_dir: Path, prepared: PreparedRun
    ) -> WorkflowOutput:
        source, out = params["source"], params["output"]
        frames, rate = source["frames"], Fraction(source["rate"])
        final = job_dir / "output.mp4"
        try:
            raw = probe_video(prepared.raw_output, self.media.ffprobe)
            whole_groups = padding(frames).groups * GROUP_STEP
            _expect("vpipe rendered", f"{raw.frames} frames", f"{whole_groups} frames")
            finalize_upscale(
                prepared.raw_output,
                job_dir / params["source_video"],
                final,
                width=out["width"],
                height=out["height"],
                frames=frames,
                rate=rate,
                audio=source["audio_codec"] is not None,
                comment=f"vpipe-job:{job_id}",
                ffmpeg=self.media.ffmpeg,
            )
            info = probe_video(final, self.media.ffprobe)
        except MediaError as exc:
            raise WorkflowFailedError("postprocess_failed", str(exc), retryable=True) from exc
        size = f"{out['width']}x{out['height']}x{frames}"
        _expect("encoded", f"{info.width}x{info.height}x{info.frames}", size)
        prepared.raw_output.unlink(missing_ok=True)
        return WorkflowOutput(file=final, result=self._result(info, raw, frames, rate))

    def _result(self, info: VideoInfo, raw: VideoInfo, frames: int, rate: Fraction) -> dict:
        return {
            "output": {
                "media_type": self.output_media_type,
                "width": info.width,
                "height": info.height,
                "frames": info.frames,
                "fps": _number(rate),
                "duration_sec": round(float(info.frames / rate), 3),
            },
            "seed_used": None,  # not a generation
            "details": {
                "generation": {
                    "width": raw.width,
                    "height": raw.height,
                    "frames": frames + padding(frames).back,
                }
            },
        }

    def progress_mapper(self, params: Mapping[str, Any]) -> ProgressMapper:
        return _GroupProgress(padding(params["source"]["frames"]).groups)

    def smoke_params(self) -> dict[str, Any]:
        """One group of a tiny clip, made on the spot (doctor --smoke needs ffmpeg anyway)."""
        cmd = [
            self.media.ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=256x144:rate=24",
        ]
        cmd += ["-frames:v", "21", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        cmd += ["-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "-"]
        clip = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
        return {
            "source_video": {"data": base64.b64encode(clip).decode(), "media_type": "video/mp4"},
            "output": {"width": 256, "height": 144},
        }


class _GroupProgress:
    """vpipe reports denoise 0..100 % once per group (each followed by its decode); this
    adds the groups up so the job's progress only moves forward."""

    def __init__(self, groups: int) -> None:
        self._groups = groups
        self._done = 0
        self._last = 0.0

    def __call__(self, phase: str, fraction: float) -> float | None:
        if phase != "denoise":
            return None
        if fraction < self._last:  # the next group started
            self._done += 1
        self._last = fraction
        return round(min(0.99, 0.02 + 0.95 * (self._done + fraction) / self._groups), 3)


def _default_output(width: int, height: int) -> OutputSize:
    scale = DEFAULT_LONG_SIDE / max(width, height)
    size = (_even(width * scale), _even(height * scale))
    if not aspect_ok(*size):
        raise InvalidParamsError("source is outside 9:16..16:9; give output explicitly")
    return OutputSize(width=size[0], height=size[1])


def _expect(what: str, got: str, expected: str) -> None:
    if got != expected:
        message = f"{what} {got}, expected {expected}"
        raise WorkflowFailedError("unexpected_output", message, retryable=False)


def _number(rate: Fraction) -> int | float:
    return rate.numerator if rate.denominator == 1 else round(float(rate), 3)
