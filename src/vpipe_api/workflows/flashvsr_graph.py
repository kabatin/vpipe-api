"""vpipe pipeline graph for FlashVSR v1.1 video super-resolution.

Mirrors vpipe's ``docs/pipelines/flashvsr-upscale-1920x1088.vpipeline`` without its audio
branch (the source's audio is put back by ffmpeg) and with a lossless intermediate.
FlashVSR works on 25-frame groups and returns 21 frames for each (the next group starts
with the last 4 frames of the previous one); width and height must be multiples of 128,
and its input is the clip already resized to that size. ``generate-video`` input ports:
0 = conditioning (from the source encoder), 2 = model.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

GROUP_FRAMES = 25
GROUP_OVERLAP = 4
GROUP_STEP = GROUP_FRAMES - GROUP_OVERLAP  # frames returned per group
GRID = 128


@dataclass(frozen=True)
class FlashVsrGraphInputs:
    source: Path  # lossless, already cropped to the output's shape and padded
    width: int  # processing size, multiples of GRID
    height: int
    rate: Fraction
    output: Path


def _port(src: str = "", oport: int = 0) -> dict[str, Any]:
    return {"src": src, "oport": oport}


def _stage(
    stage_id: str, stage_type: str, iports: list[dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    return {"id": stage_id, "type": stage_type, "iports": iports, "config": config}


def build_flashvsr_spec(
    pipeline_id: str, model_key: str, inp: FlashVsrGraphInputs
) -> dict[str, Any]:
    if inp.width % GRID or inp.height % GRID:
        raise ValueError(f"FlashVSR needs multiples of {GRID}, got {inp.width}x{inp.height}")
    fps = float(inp.rate)  # vpipe stamps this; the final encode sets the exact rate
    stages = [
        _stage("model-select", "model-select", [], {"hf_dir": model_key}),
        _stage(
            "load-video",
            "load-video",
            [],
            {"input_url": str(inp.source), "enable_audio": False},
        ),
        _stage(
            "video-to-rgb",
            "video-to-rgb",
            [_port("load-video")],
            {"output_dtype": "u8", "hwaccel": "auto", "oport_capacity": 2},
        ),
        _stage(
            "upscale-in",
            "image-resample",
            [_port("video-to-rgb")],
            {"width": inp.width, "height": inp.height, "fit": "stretch", "algorithm": "lanczos"},
        ),
        _stage(
            "temporal-stack",
            "temporal-stack",
            [_port("upscale-in")],
            {
                "mode": "video",
                "group_size": GROUP_FRAMES,
                "overlap": GROUP_OVERLAP,
                "max_mb": 512,
            },
        ),
        _stage(
            "flashvsr-src-encoder",
            "flashvsr-src-encoder",
            [_port("temporal-stack"), _port("model-select")],
            {},
        ),
        _stage(
            "generate-video",
            "generate-video",
            [_port("flashvsr-src-encoder"), _port(), _port("model-select")],
            {
                "width": inp.width,
                "height": inp.height,
                "frames": GROUP_FRAMES,
                "seed": 0,
                "fps": fps,
                "unload_when_idle": "auto",
            },
        ),
        _stage(
            "vae-decode",
            "vae-decode",
            [_port("generate-video"), _port("model-select")],
            {},
        ),
        # lossless intermediate, as for H3: the final encode is the only lossy step
        _stage(
            "rgb-to-video",
            "rgb-to-video",
            [_port("vae-decode")],
            {"fps": fps, "pix_fmt": "yuv444p", "color_range": "full", "colorspace": "bt709"},
        ),
        _stage(
            "save-video",
            "save-video",
            [_port("rgb-to-video")],
            {
                "output_url": str(inp.output),
                "enable_video": True,
                "enable_audio": False,
                "video_codec": "ffv1",
            },
        ),
    ]
    return {"id": pipeline_id, "stages": stages, "subpipelines": []}
