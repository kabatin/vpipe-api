"""Workflow ``minimax-h3-turbo-video``: MiniMax H3 (FL2VA 8-bit) + Turbo LoRA.

Text to video, optionally anchored to a first frame (and a last frame). The clip is
generated at a size tier that keeps the run practical on Apple Silicon, then scaled to
exactly the requested output size. Audio is always dropped.
"""

from __future__ import annotations

import base64
import math
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vpipe_api.media import MediaError, finalize_video, normalize_image, probe_video
from vpipe_api.workflows.base import (
    InvalidParamsError,
    MediaTools,
    PreparedRun,
    RequiredModel,
    Workflow,
    WorkflowFailedError,
    WorkflowOutput,
)
from vpipe_api.workflows.h3_graph import FPS, H3GraphInputs, H3GraphOptions, build_h3_spec
from vpipe_api.workflows.inputs import OutputSize, decode_base64

MIN_FRAMES = 56  # 17*3+5  -> 2.33 s
MAX_FRAMES = 243  # 17*14+5 -> 10.125 s
MAX_IMAGE_BYTES = 20 * 1024 * 1024

Quality = Literal["draft", "standard", "final"]

# (aspect ratio, {quality: (width, height)}); all multiples of 32 inside H3's 16:9..9:16.
# "final" is H3's training resolution (short side 768).
GENERATION_SIZES: tuple[tuple[float, dict[str, tuple[int, int]]], ...] = (
    (16 / 9, {"draft": (832, 480), "standard": (1024, 576), "final": (1344, 768)}),
    (9 / 16, {"draft": (480, 832), "standard": (576, 1024), "final": (768, 1344)}),
    (1.0, {"draft": (640, 640), "standard": (768, 768), "final": (768, 768)}),
    (4 / 5, {"draft": (512, 640), "standard": (640, 800), "final": (768, 960)}),
)

# Measured on an M5 (10-core GPU, 32 GB), 6 steps: ~140 jobs at 832x480 (56 frames
# = 256 s ... 124 = 477 s ... 243 = 998 s, medians), 1024x576x124 = 640 s,
# 1024x576x243 = 1460 s, 1344x768x124 = 1338 s, 1344x768x243 = 3213 s.
# t = 150 + 330 * x^1.36 with x = pixels*frames relative to 832x480x124 fits them
# within ~4% rms (runs drift ~10% from day to day); the denoise share scales with steps.
_BASE_WORK = 832 * 480 * 124


def generation_size(width: int, height: int, quality: str) -> tuple[int, int]:
    ratio = width / height
    _, sizes = min(GENERATION_SIZES, key=lambda row: abs(math.log(ratio / row[0])))
    return sizes[quality]


class ImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: str = Field(description="base64-encoded image", json_schema_extra={"format": "base64"})
    media_type: Literal["image/png", "image/jpeg", "image/webp"]

    @field_validator("data")
    @classmethod
    def _decodable(cls, value: str) -> str:
        decode_base64(value, MAX_IMAGE_BYTES, "image")
        return value

    def decoded(self) -> bytes:
        return base64.b64decode(self.data, validate=True)


class H3VideoParams(BaseModel):
    """POST body for ``minimax-h3-turbo-video``."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=4000)
    output: OutputSize
    frames: int = Field(default=124, ge=MIN_FRAMES, le=MAX_FRAMES, description="17n+5")
    quality: Quality = Field(
        default="standard",
        description="generation size tier: draft (fast preview), standard, or final "
        "(H3's training resolution, short side 768; about 3x the time of draft at 16:9)",
    )
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    steps: int = Field(default=6, ge=4, le=8)
    start_image: ImageInput | None = None
    end_image: ImageInput | None = None

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("prompt is blank")
        return stripped

    @field_validator("frames")
    @classmethod
    def _frames(cls, value: int) -> int:
        if (value - 5) % 17 != 0:
            raise ValueError("frames must be 17n+5 (56, 73, 90, ..., 243)")
        return value

    @model_validator(mode="after")
    def _anchors(self) -> H3VideoParams:
        if self.end_image is not None and self.start_image is None:
            raise ValueError("end_image requires start_image")
        return self


class H3Options(BaseModel):
    """``[workflows."minimax-h3-turbo-video"]`` in the config file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_key: str = "local/MiniMax-H3-FL2VA-8bit"
    lora: str = "larryvrh/MiniMax-H3-Turbo-Lora-v4-600-ema"
    lora_scale: float = Field(default=1.0, gt=0, le=2)
    video_shift: float = 12.0
    audio_shift: float = 3.0
    i8_gemm: bool = True
    sol_attn: bool = True


_DEFAULT_MODELS = (
    RequiredModel(
        key="local/MiniMax-H3-FL2VA-8bit",
        path="local/MiniMax-H3-FL2VA-8bit",
        files=(
            "diffusion_models/model.safetensors.index.json",
            "text_encoders/model.safetensors.index.json",
            "vae/minimax_h3_video_vae_fp16.safetensors",
        ),
        prepare_pipelines=("prepare-minimax-h3-8bit",),
        disk_gb_needed=185,
    ),
    RequiredModel(
        key="larryvrh/MiniMax-H3-Turbo-Lora-v4-600-ema",
        path="larryvrh/MiniMax-H3-Turbo-Lora",
        files=("minimax_h3_turbo_v4_step600_ema.safetensors",),
        prepare_pipelines=("prepare-minimax-h3-turbo-lora",),
        disk_gb_needed=1,
    ),
)


class H3VideoWorkflow(Workflow):
    id = "minimax-h3-turbo-video"
    description = (
        "MiniMax H3 (FL2VA, 8-bit) + Turbo LoRA: text or first/last-frame to video, "
        "scaled to the requested size, no audio"
    )
    output_media_type = "video/mp4"
    params_model = H3VideoParams

    def __init__(self, media: MediaTools, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(media)
        self.options = H3Options.model_validate(dict(options or {}))

    @property
    def required_models(self) -> tuple[RequiredModel, ...]:
        """Known models are checked file-by-file; a custom ``model_key`` only by directory.

        A custom ``lora`` key cannot be mapped to a directory, so it is not checked.
        """
        selected = (self.options.model_key, self.options.lora)
        models = [model for model in _DEFAULT_MODELS if model.key in selected]
        if self.options.model_key not in {model.key for model in _DEFAULT_MODELS}:
            models.append(RequiredModel(key=self.options.model_key, path=self.options.model_key))
        return tuple(models)

    def store_inputs(self, params: BaseModel, job_dir: Path) -> dict[str, Any]:
        p = cast(H3VideoParams, params)
        stored = p.model_dump(exclude={"start_image", "end_image"})
        stored["seed"] = p.seed if p.seed is not None else secrets.randbelow(2**31)
        inputs = job_dir / "inputs"
        for name, image in (("start_image", p.start_image), ("end_image", p.end_image)):
            if image is None:
                stored[name] = None
                continue
            try:
                png = normalize_image(image.decoded(), image.media_type)
            except MediaError as exc:
                raise InvalidParamsError(f"{name}: {exc}") from exc
            inputs.mkdir(parents=True, exist_ok=True)
            path = inputs / f"{name}.png"
            path.write_bytes(png)
            stored[name] = str(path.relative_to(job_dir))
        return stored

    def estimate_seconds(self, params: Mapping[str, Any]) -> float:
        out = params["output"]
        width, height = generation_size(out["width"], out["height"], params["quality"])
        work = width * height * params["frames"] / _BASE_WORK
        step_factor = 0.23 + 0.77 * params["steps"] / 6
        return 150 + 330 * work**1.36 * step_factor

    def prepare(self, job_id: str, params: Mapping[str, Any], job_dir: Path) -> PreparedRun:
        out = params["output"]
        width, height = generation_size(out["width"], out["height"], params["quality"])
        # Matroska, FFV1's own container: ffmpeg before 7 cannot put FFV1 in MP4
        raw = job_dir / "raw.mkv"

        def anchor(name: str) -> Path | None:
            rel = params.get(name)
            return None if rel is None else (job_dir / rel).resolve()

        spec = build_h3_spec(
            f"vpipe-api-{job_id}",
            H3GraphOptions(**self.options.model_dump()),
            H3GraphInputs(
                prompt=params["prompt"],
                width=width,
                height=height,
                frames=params["frames"],
                steps=params["steps"],
                seed=params["seed"],
                output=raw.resolve(),
                first_frame=anchor("start_image"),
                last_frame=anchor("end_image"),
            ),
        )
        return PreparedRun(spec=spec, raw_output=raw.resolve())

    def finalize(
        self, job_id: str, params: Mapping[str, Any], job_dir: Path, prepared: PreparedRun
    ) -> WorkflowOutput:
        out = params["output"]
        final = job_dir / "output.mp4"
        try:
            raw = probe_video(prepared.raw_output, self.media.ffprobe)
            if raw.frames != params["frames"]:
                raise WorkflowFailedError(
                    "unexpected_output",
                    f"vpipe rendered {raw.frames} frames, expected {params['frames']}",
                    retryable=False,
                )
            finalize_video(
                prepared.raw_output,
                final,
                width=out["width"],
                height=out["height"],
                fps=FPS,
                comment=f"vpipe-job:{job_id}",
                ffmpeg=self.media.ffmpeg,
            )
            info = probe_video(final, self.media.ffprobe)
        except MediaError as exc:
            raise WorkflowFailedError("postprocess_failed", str(exc), retryable=True) from exc
        prepared.raw_output.unlink(missing_ok=True)
        return WorkflowOutput(
            file=final,
            result={
                "output": {
                    "media_type": self.output_media_type,
                    "width": info.width,
                    "height": info.height,
                    "frames": info.frames,
                    "fps": FPS,
                    "duration_sec": round(info.frames / FPS, 3),
                },
                "seed_used": params["seed"],
                "details": {
                    "generation": {
                        "width": raw.width,
                        "height": raw.height,
                        "frames": raw.frames,
                        "steps": params["steps"],
                        "quality": params["quality"],
                    }
                },
            },
        )

    def smoke_params(self) -> dict[str, Any]:
        return {
            "prompt": "A small wooden boat drifting on a calm lake at dawn, gentle ripples.",
            "output": {"width": 832, "height": 480},
            "frames": MIN_FRAMES,
            "quality": "draft",
            "steps": 4,
            "seed": 1,
        }
