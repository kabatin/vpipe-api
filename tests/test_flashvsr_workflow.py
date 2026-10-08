import base64
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import needs_ffmpeg
from vpipe_api.media import probe_video
from vpipe_api.workflows.base import InvalidParamsError, MediaTools, PreparedRun
from vpipe_api.workflows.flashvsr import (
    MAX_SOURCE_BYTES,
    FlashVsrParams,
    FlashVsrWorkflow,
    cover_crop,
    padding,
    processing_size,
)
from vpipe_api.workflows.flashvsr_graph import FlashVsrGraphInputs, build_flashvsr_spec


def _clip(path: Path, *, size: str = "320x180", frames: int = 30, rate: str = "24") -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate={rate}",
            "-frames:v",
            str(frames),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path


def _video(path: Path) -> dict[str, str]:
    return {"data": base64.b64encode(path.read_bytes()).decode(), "media_type": "video/mp4"}


def _params(path: Path, **extra: object) -> FlashVsrParams:
    return FlashVsrParams.model_validate({"source_video": _video(path), **extra})


@pytest.mark.parametrize(
    ("out", "expected"),
    [
        ((1920, 1080), (1920, 1152)),  # vpipe's own 1920x1088 pipeline works at 1920x1152
        ((1080, 1920), (1152, 1920)),
        ((1920, 1088), (1920, 1152)),
        ((1280, 720), (1280, 768)),  # smaller outputs are processed smaller (faster)
        ((1080, 1080), (1152, 1152)),
        ((64, 64), (128, 128)),
        ((3840, 2160), (1920, 1152)),  # larger outputs: the most pixels within the cap,
        ((2160, 3840), (1152, 1920)),  # no more stretched than 1920x1080 itself
        ((2560, 1440), (1920, 1152)),
        ((4096, 4096), (1408, 1408)),
    ],
)
def test_processing_size_is_on_the_128_grid(
    out: tuple[int, int], expected: tuple[int, int]
) -> None:
    width, height = processing_size(*out)
    assert (width, height) == expected
    assert width % 128 == 0 and height % 128 == 0 and width * height <= 1920 * 1152


@pytest.mark.parametrize(
    ("source", "out", "expected"),
    [
        ((1920, 1080), (1920, 1080), (1920, 1080)),
        ((1344, 768), (1920, 1080), (1344, 756)),  # 1.75 -> 16:9: trim 12 rows
        ((1920, 1080), (1920, 1088), (1904, 1080)),
        ((1080, 1920), (1080, 1920), (1080, 1920)),
        ((1920, 1080), (1080, 1080), (1080, 1080)),
        ((183, 183), (1080, 1080), (182, 182)),  # odd 4:4:4 sizes: never past the source
    ],
)
def test_cover_crop_keeps_the_output_shape(
    source: tuple[int, int], out: tuple[int, int], expected: tuple[int, int]
) -> None:
    width, height = cover_crop(*source, *out)
    assert (width, height) == expected
    assert width % 2 == 0 and height % 2 == 0
    assert width <= source[0] and height <= source[1]
    assert abs(width / height - out[0] / out[1]) < 0.01


@pytest.mark.parametrize(("frames", "groups"), [(1, 1), (21, 1), (22, 2), (56, 3), (243, 12)])
def test_padding_covers_every_frame_with_whole_groups(frames: int, groups: int) -> None:
    pad = padding(frames)
    assert pad.groups == groups
    total = frames + pad.back
    assert total == 21 * groups + 4  # vpipe returns 21 frames per 25-frame group
    assert 21 * groups >= frames  # every real frame comes back


def test_params_validation(tmp_path: Path) -> None:
    video = {"data": base64.b64encode(b"\0" * 10).decode(), "media_type": "video/mp4"}
    assert FlashVsrParams.model_validate({"source_video": video}).output is None
    with pytest.raises(ValidationError):
        FlashVsrParams.model_validate({"source_video": {**video, "media_type": "video/webm"}})
    with pytest.raises(ValidationError):
        FlashVsrParams.model_validate(
            {"source_video": video, "output": {"width": 3000, "height": 1000}}
        )
    big = base64.b64encode(b"\0" * (MAX_SOURCE_BYTES + 1)).decode()
    with pytest.raises(ValidationError, match="larger than 64 MB"):
        FlashVsrParams.model_validate({"source_video": {**video, "data": big}})


@needs_ffmpeg
def test_store_inputs_probes_the_source(tmp_path: Path) -> None:
    wf = FlashVsrWorkflow(MediaTools())
    src = _clip(tmp_path / "s.mp4", size="1344x768", frames=56)
    stored = wf.store_inputs(_params(src, output={"width": 1920, "height": 1080}), tmp_path / "job")
    assert stored["source_video"] == "inputs/source.mp4"
    assert stored["source"] == {
        "width": 1344,
        "height": 768,
        "frames": 56,
        "rate": "24",
        "audio_codec": None,
    }
    assert stored["output"] == {"width": 1920, "height": 1080}
    assert (tmp_path / "job" / "inputs" / "source.mp4").is_file()
    assert "data" not in str(stored)


@needs_ffmpeg
@pytest.mark.parametrize(
    ("size", "expected"), [("1344x768", (1920, 1098)), ("768x1344", (1098, 1920))]
)
def test_store_inputs_defaults_the_output_to_the_source_shape(
    tmp_path: Path, size: str, expected: tuple[int, int]
) -> None:
    stored = FlashVsrWorkflow(MediaTools()).store_inputs(
        _params(_clip(tmp_path / "s.mp4", size=size, frames=5)), tmp_path / "job"
    )
    assert (stored["output"]["width"], stored["output"]["height"]) == expected


@needs_ffmpeg
def test_store_inputs_rejects_unusable_sources(tmp_path: Path) -> None:
    wf = FlashVsrWorkflow(MediaTools())
    garbage = tmp_path / "g.mp4"
    garbage.write_bytes(b"not a video")
    with pytest.raises(InvalidParamsError, match="not a readable MP4"):
        wf.store_inputs(_params(garbage), tmp_path / "a")
    long = _clip(tmp_path / "l.mp4", size="64x64", frames=24 * 41)
    with pytest.raises(InvalidParamsError, match="40 s"):
        wf.store_inputs(_params(long), tmp_path / "b")


def _stored(**overrides: object) -> dict:
    base = {
        "source_video": "inputs/source.mp4",
        "source": {"width": 1344, "height": 768, "frames": 56, "rate": "24", "audio_codec": None},
        "output": {"width": 1920, "height": 1080},
    }
    return base | overrides


def test_estimate_grows_with_length_and_size() -> None:
    wf = FlashVsrWorkflow(MediaTools())
    short = wf.estimate_seconds(_stored())
    long = wf.estimate_seconds(_stored(source={**_stored()["source"], "frames": 192}))
    small = wf.estimate_seconds(_stored(output={"width": 1280, "height": 720}))
    assert small < short < long
    two_groups = wf.estimate_seconds(_stored(source={**_stored()["source"], "frames": 42}))
    assert 190 < two_groups < 230  # measured 210 s for 2 groups at 1920x1152
    assert 285 < short < 345  # 56 frames = 3 groups; measured 314 s
    # vpipe reloads the model for every group whatever the size: small outputs keep a floor
    tiny = wf.estimate_seconds(
        _stored(source={**_stored()["source"], "frames": 2400}, output={"width": 64, "height": 64})
    )
    assert tiny / padding(2400).groups >= 30


def test_graph_mirrors_vpipes_pipeline(tmp_path: Path) -> None:
    spec = build_flashvsr_spec(
        "p",
        "JunhaoZhuang/FlashVSR-v1.1",
        FlashVsrGraphInputs(
            source=tmp_path / "padded.mkv",
            width=1920,
            height=1152,
            rate=Fraction(24000, 1001),
            output=tmp_path / "raw.mkv",
        ),
    )
    stages = {s["id"]: s for s in spec["stages"]}
    assert list(stages) == [
        "model-select",
        "load-video",
        "video-to-rgb",
        "upscale-in",
        "temporal-stack",
        "flashvsr-src-encoder",
        "generate-video",
        "vae-decode",
        "rgb-to-video",
        "save-video",
    ]
    assert stages["model-select"]["config"] == {"hf_dir": "JunhaoZhuang/FlashVSR-v1.1"}
    assert stages["load-video"]["config"]["input_url"] == str(tmp_path / "padded.mkv")
    assert stages["upscale-in"]["config"] == {
        "width": 1920,
        "height": 1152,
        "fit": "stretch",
        "algorithm": "lanczos",
    }
    stack = stages["temporal-stack"]["config"]
    assert (stack["group_size"], stack["overlap"]) == (25, 4)
    gen = stages["generate-video"]["config"]
    assert (gen["width"], gen["height"], gen["frames"]) == (1920, 1152, 25)
    assert gen["fps"] == pytest.approx(23.976, abs=1e-3)
    rgb = stages["rgb-to-video"]["config"]
    assert (rgb["pix_fmt"], rgb["color_range"], rgb["colorspace"]) == ("yuv444p", "full", "bt709")
    save = stages["save-video"]["config"]
    assert (save["video_codec"], save["enable_audio"]) == ("ffv1", False)


@needs_ffmpeg
def test_prepare_pads_crops_and_points_vpipe_at_the_copy(tmp_path: Path) -> None:
    wf = FlashVsrWorkflow(MediaTools())
    job = tmp_path / "job"
    src = _clip(tmp_path / "s.mp4", size="1344x768", frames=30)
    stored = wf.store_inputs(_params(src, output={"width": 1920, "height": 1080}), job)
    prepared = wf.prepare("job_x", stored, job)
    stages = {s["id"]: s for s in prepared.spec["stages"]}
    padded = Path(stages["load-video"]["config"]["input_url"])
    assert padded.is_absolute() and padded.parent == (job / "inputs").resolve()
    info = probe_video(padded)
    assert (info.width, info.height, info.frames) == (1344, 756, 21 * 2 + 4)
    assert (
        stages["generate-video"]["config"]["width"],
        stages["upscale-in"]["config"]["height"],
    ) == (
        1920,
        1152,
    )
    assert prepared.raw_output == (job / "raw.mkv").resolve()


@needs_ffmpeg
def test_finalize_returns_the_source_length_at_the_source_rate(tmp_path: Path) -> None:
    wf = FlashVsrWorkflow(MediaTools())
    job = tmp_path / "job"
    src = _clip(tmp_path / "s.mp4", size="1344x768", frames=30, rate="24000/1001")
    stored = wf.store_inputs(_params(src, output={"width": 1920, "height": 1080}), job)
    raw = job / "raw.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1920x1152:rate=24",
            "-frames:v",
            "42",
            "-c:v",
            "ffv1",
            str(raw),
        ],
        check=True,
    )
    out = wf.finalize("job_x", stored, job, PreparedRun(spec={}, raw_output=raw))
    assert out.result["output"] == {
        "media_type": "video/mp4",
        "width": 1920,
        "height": 1080,
        "frames": 30,
        "fps": 23.976,
        "duration_sec": 1.251,
    }
    assert out.result["seed_used"] is None
    assert out.result["details"]["generation"] == {"width": 1920, "height": 1152, "frames": 46}
    assert not raw.exists()


@needs_ffmpeg
def test_finalize_refuses_a_short_render(tmp_path: Path) -> None:
    from vpipe_api.workflows.base import WorkflowFailedError

    wf = FlashVsrWorkflow(MediaTools())
    job = tmp_path / "job"
    stored = wf.store_inputs(_params(_clip(tmp_path / "s.mp4", frames=30)), job)
    raw = job / "raw.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1920x1152:rate=24",
            "-frames:v",
            "21",
            "-c:v",
            "ffv1",
            str(raw),
        ],
        check=True,
    )
    with pytest.raises(WorkflowFailedError, match="21 frames, expected 42"):
        wf.finalize("job_x", stored, job, PreparedRun(spec={}, raw_output=raw))


def test_required_models_are_fetched_from_the_hub() -> None:
    (model,) = FlashVsrWorkflow(MediaTools()).required_models
    assert (model.key, model.hf_fetch) == ("JunhaoZhuang/FlashVSR-v1.1", True)
    assert "posi_prompt.pth" in model.files


@needs_ffmpeg
def test_prepare_refuses_a_preprocessed_copy_of_the_wrong_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vpipe_api.workflows import flashvsr
    from vpipe_api.workflows.base import WorkflowFailedError

    wf = FlashVsrWorkflow(MediaTools())
    job = tmp_path / "job"
    stored = wf.store_inputs(_params(_clip(tmp_path / "s.mp4", frames=30)), job)

    def short_copy(src: Path, dst: Path, **_: object) -> None:
        _clip(dst.with_suffix(".mp4"), frames=5).replace(dst)

    monkeypatch.setattr(flashvsr, "pad_and_crop_source", short_copy)
    with pytest.raises(WorkflowFailedError, match="decoded to"):
        wf.prepare("job_x", stored, job)


def test_progress_adds_up_the_groups() -> None:
    track = FlashVsrWorkflow(MediaTools()).progress_mapper(_stored())  # 56 frames = 3 groups
    seen = [
        track(phase, fraction)
        for phase, fraction in [
            ("denoise", 0.0),
            ("denoise", 1.0),
            ("vae decode", 0.5),  # vpipe's per-group decode: not a separate phase here
            ("denoise", 0.0),
            ("denoise", 0.5),
            ("denoise", 1.0),
            ("denoise", 0.0),
            ("denoise", 1.0),
        ]
    ]
    values = [v for v in seen if v is not None]
    assert seen[2] is None
    assert values == sorted(values)  # never goes backwards between groups
    assert values[0] == 0.02 and values[-1] == 0.97
    assert values[3] == pytest.approx(0.02 + 0.95 * 1.5 / 3, abs=1e-3)
