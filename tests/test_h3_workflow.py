import base64
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import make_png
from vpipe_api.workflows.base import InvalidParamsError, MediaTools
from vpipe_api.workflows.h3_graph import H3GraphInputs, H3GraphOptions, build_h3_spec
from vpipe_api.workflows.h3_video import (
    MAX_IMAGE_BYTES,
    H3Options,
    H3VideoParams,
    H3VideoWorkflow,
    generation_size,
)

BASE = {"prompt": "a lake", "output": {"width": 1920, "height": 1080}}


def params(**overrides: object) -> H3VideoParams:
    return H3VideoParams.model_validate(BASE | overrides)


def png_b64(tmp_path: Path) -> str:
    return base64.b64encode(make_png(tmp_path / "i.png")).decode()


@pytest.mark.parametrize("frames", [56, 73, 124, 243])
def test_valid_frames(frames: int) -> None:
    assert params(frames=frames).frames == frames


@pytest.mark.parametrize("frames", [55, 60, 120, 260])
def test_invalid_frames(frames: int) -> None:
    with pytest.raises(ValidationError):
        params(frames=frames)


def test_defaults_and_prompt_strip() -> None:
    p = params(prompt="  sunset  ")
    assert (p.prompt, p.frames, p.quality, p.steps, p.seed) == ("sunset", 124, "standard", 6, None)


@pytest.mark.parametrize(
    "bad",
    [
        {"prompt": "   "},
        {"output": {"width": 2560, "height": 1080}},
        {"steps": 9},
        {"seed": -1},
        {"quality": "ultra"},
        {"extra": 1},
    ],
)
def test_rejected_params(bad: dict) -> None:
    with pytest.raises(ValidationError):
        params(**bad)


def test_end_image_requires_start(tmp_path: Path) -> None:
    image = {"data": png_b64(tmp_path), "media_type": "image/png"}
    with pytest.raises(ValidationError, match="requires start_image"):
        params(end_image=image)
    assert params(start_image=image, end_image=image).end_image is not None


@pytest.mark.parametrize("data", ["not base64!!", "", base64.b64encode(b"").decode()])
def test_bad_image_data(data: str) -> None:
    with pytest.raises(ValidationError):
        params(start_image={"data": data, "media_type": "image/png"})


def test_image_size_limit() -> None:
    big = base64.b64encode(b"\0" * (MAX_IMAGE_BYTES + 1)).decode()
    with pytest.raises(ValidationError, match="larger than"):
        params(start_image={"data": big, "media_type": "image/png"})


@pytest.mark.parametrize(
    ("out", "quality", "expected"),
    [
        ((1920, 1080), "draft", (832, 480)),
        ((1280, 720), "standard", (1024, 576)),
        ((1080, 1920), "standard", (576, 1024)),
        ((1080, 1080), "draft", (640, 640)),
        ((1080, 1350), "standard", (640, 800)),
        ((864, 1080), "draft", (512, 640)),
    ],
)
def test_generation_size(out: tuple[int, int], quality: str, expected: tuple[int, int]) -> None:
    assert generation_size(*out, quality) == expected


def test_estimate_grows_with_work() -> None:
    wf = H3VideoWorkflow(MediaTools())
    draft = wf.estimate_seconds(params(quality="draft").model_dump())
    standard = wf.estimate_seconds(params().model_dump())
    long = wf.estimate_seconds(params(frames=243).model_dump())
    assert 350 < draft < standard < long
    assert 400 < draft < 450  # measured ~420 s for 832x480x124 at 6 steps


def test_store_inputs_writes_png_and_seed(tmp_path: Path) -> None:
    wf = H3VideoWorkflow(MediaTools())
    image = {"data": png_b64(tmp_path), "media_type": "image/png"}
    stored = wf.store_inputs(params(start_image=image), tmp_path / "job")
    assert stored["start_image"] == "inputs/start_image.png"
    assert stored["end_image"] is None
    assert isinstance(stored["seed"], int)
    assert (tmp_path / "job" / "inputs" / "start_image.png").read_bytes()[:4] == b"\x89PNG"
    assert "data" not in str(stored)


def test_store_inputs_rejects_mismatched_media_type(tmp_path: Path) -> None:
    wf = H3VideoWorkflow(MediaTools())
    image = {"data": png_b64(tmp_path), "media_type": "image/jpeg"}
    with pytest.raises(InvalidParamsError, match="does not match"):
        wf.store_inputs(params(start_image=image), tmp_path / "job")


def test_prepare_builds_first_last_graph(tmp_path: Path) -> None:
    wf = H3VideoWorkflow(MediaTools(), {"sol_attn": False})
    image = {"data": png_b64(tmp_path), "media_type": "image/png"}
    job = tmp_path / "job"
    stored = wf.store_inputs(params(start_image=image, end_image=image, seed=5), job)
    prepared = wf.prepare("job_x", stored, job)
    stages = {s["id"]: s for s in prepared.spec["stages"]}
    ports = stages["generate-video"]["iports"]
    assert ports[5]["src"] == "vae-encode-first" and ports[6]["src"] == "vae-encode-last"
    assert stages["generate-video"]["config"]["sol_attn"] is False
    assert stages["generate-video"]["config"]["seed"] == 5
    assert stages["first-frame"]["config"]["width"] == 1024
    assert Path(stages["load-first"]["config"]["url"][0]).is_absolute()
    assert prepared.raw_output.is_absolute()


def test_graph_text_only_leaves_anchor_ports_empty(tmp_path: Path) -> None:
    opts = H3GraphOptions(**H3Options().model_dump())
    spec = build_h3_spec(
        "p",
        opts,
        H3GraphInputs(
            prompt="日本語🌊",
            width=832,
            height=480,
            frames=56,
            steps=4,
            seed=1,
            output=tmp_path / "o.mp4",
        ),
    )
    gen = next(s for s in spec["stages"] if s["id"] == "generate-video")
    assert [p["src"] for p in gen["iports"]][5:7] == ["", ""]
    assert gen["iports"][9]["src"] == "minimax-h3-model-config"
    assert not any(s["type"] == "vae-encode" for s in spec["stages"])
    with pytest.raises(ValueError, match="first frame"):
        build_h3_spec(
            "p",
            opts,
            H3GraphInputs(
                prompt="x",
                width=832,
                height=480,
                frames=56,
                steps=4,
                seed=1,
                output=tmp_path / "o.mp4",
                last_frame=tmp_path / "l.png",
            ),
        )


def test_required_models_follow_options() -> None:
    assert len(H3VideoWorkflow(MediaTools()).required_models) == 2
    custom = H3VideoWorkflow(MediaTools(), {"model_key": "local/Other-4bit"}).required_models
    assert [m.key for m in custom][-1] == "local/Other-4bit"


def test_unknown_option_rejected() -> None:
    with pytest.raises(ValidationError):
        H3VideoWorkflow(MediaTools(), {"bogus": 1})
