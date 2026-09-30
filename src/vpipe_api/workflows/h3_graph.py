"""vpipe pipeline graphs for MiniMax H3 (FL2VA) with a runtime Turbo LoRA.

Mirrors vpipe's ``docs/pipelines/minimax-h3-text-to-video-turbo.vpipeline`` and
``minimax-h3-first-last-to-video.vpipeline``. ``generate-video`` input ports:
0 = conditioning, 2 = model, 5 = first-frame latent, 6 = last-frame latent, 9 = H3 config.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENERATE_VIDEO_PORTS = 10
PORT_CONDITIONING = 0
PORT_MODEL = 2
PORT_FIRST_FRAME = 5
PORT_LAST_FRAME = 6
PORT_H3_CONFIG = 9
FPS = 24


@dataclass(frozen=True)
class H3GraphOptions:
    model_key: str
    lora: str
    lora_scale: float
    video_shift: float
    audio_shift: float
    i8_gemm: bool
    sol_attn: bool


@dataclass(frozen=True)
class H3GraphInputs:
    prompt: str
    width: int
    height: int
    frames: int
    steps: int
    seed: int
    output: Path
    first_frame: Path | None = None
    last_frame: Path | None = None


def _port(src: str = "", oport: int = 0) -> dict[str, Any]:
    return {"src": src, "oport": oport}


def _stage(
    stage_id: str, stage_type: str, iports: list[dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    return {"id": stage_id, "type": stage_type, "iports": iports, "config": config}


def _anchor_stages(name: str, image: Path, width: int, height: int) -> list[dict[str, Any]]:
    """load-image -> image-resample (lanczos crop to the generation size) -> vae-encode.

    Anchors must be encoded at the clip's own resolution; lanczos keeps the detail that
    the VAE would otherwise encode as intentional softness.
    """
    return [
        _stage(f"load-{name}", "load-image", [], {"url": [str(image)]}),
        _stage(
            f"{name}-frame",
            "image-resample",
            [_port(f"load-{name}")],
            {"width": width, "height": height, "fit": "crop", "algorithm": "lanczos"},
        ),
        _stage(
            f"vae-encode-{name}",
            "vae-encode",
            [_port(f"{name}-frame"), _port("model-select")],
            {"unload_when_idle": "always"},
        ),
    ]


def build_h3_spec(pipeline_id: str, opts: H3GraphOptions, inp: H3GraphInputs) -> dict[str, Any]:
    if inp.last_frame is not None and inp.first_frame is None:
        raise ValueError("a last frame needs a first frame")

    gen_ports = [_port() for _ in range(GENERATE_VIDEO_PORTS)]
    gen_ports[PORT_CONDITIONING] = _port("diffusion-conditioner")
    gen_ports[PORT_MODEL] = _port("model-select")
    gen_ports[PORT_H3_CONFIG] = _port("minimax-h3-model-config")

    stages: list[dict[str, Any]] = [
        _stage("model-select", "model-select", [], {"hf_dir": opts.model_key}),
        _stage("text-prompt", "text-prompt", [], {"text": inp.prompt}),
        _stage(
            "diffusion-conditioner",
            "diffusion-conditioner",
            [_port("text-prompt"), _port(), _port("model-select")],
            {"unload_when_idle": "always"},
        ),
        _stage(
            "minimax-h3-model-config",
            "minimax-h3-model-config",
            [],
            {
                "video_shift": opts.video_shift,
                "audio_shift": opts.audio_shift,
                "condition_timestep": 1.0,
                "audio_seconds": 0.0,
                "lora": opts.lora,
                "lora_scale": opts.lora_scale,
            },
        ),
    ]
    if inp.first_frame is not None:
        stages += _anchor_stages("first", inp.first_frame, inp.width, inp.height)
        gen_ports[PORT_FIRST_FRAME] = _port("vae-encode-first")
    if inp.last_frame is not None:
        stages += _anchor_stages("last", inp.last_frame, inp.width, inp.height)
        gen_ports[PORT_LAST_FRAME] = _port("vae-encode-last")

    stages += [
        _stage(
            "generate-video",
            "generate-video",
            gen_ports,
            {
                "width": inp.width,
                "height": inp.height,
                "frames": inp.frames,
                "fps": FPS,
                "steps": inp.steps,
                "seed": inp.seed,
                "i8_gemm": opts.i8_gemm,
                "sol_attn": opts.sol_attn,
                "unload_when_idle": "always",
            },
        ),
        _stage(
            "audio-vae-decode",
            "audio-vae-decode",
            [_port("generate-video", 1), _port("model-select")],
            {},
        ),
        _stage("vae-decode", "vae-decode", [_port("generate-video", 0), _port("model-select")], {}),
        _stage("rgb-to-video", "rgb-to-video", [_port("vae-decode")], {"fps": FPS}),
        _stage(
            "save-video",
            "save-video",
            [_port("rgb-to-video"), _port("audio-vae-decode")],
            {"output_url": str(inp.output), "enable_video": True, "enable_audio": True},
        ),
    ]
    return {"id": pipeline_id, "stages": stages, "subpipelines": []}
