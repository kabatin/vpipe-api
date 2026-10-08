"""The real workflows, real runner and real ffmpeg, against the fake vpipe."""

import base64
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import install_models, make_png, needs_ffmpeg
from vpipe_api.api.app import create_app
from vpipe_api.jobs.queue import JobQueue
from vpipe_api.jobs.store import JobStore
from vpipe_api.media import probe_video
from vpipe_api.runner import VpipeRunner
from vpipe_api.settings import Settings
from vpipe_api.workflows import build_registry


@needs_ffmpeg
def test_first_frame_job_over_http(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_VPIPE_CALLS", str(calls))
    vpipe_bin, work_dir = settings.require_runtime()
    registry = build_registry(settings)
    queue = JobQueue(
        JobStore(settings.data_dir),
        registry,
        VpipeRunner(vpipe_bin, work_dir),
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
    )
    app = create_app(queue, registry, token=None, max_body_bytes=64 << 20)
    image = base64.b64encode(make_png(tmp_path / "s.png", (320, 240))).decode()

    with TestClient(app, base_url="http://127.0.0.1") as client:
        job = client.post(
            "/v1/workflows/minimax-h3-turbo-video/jobs",
            json={
                "prompt": "波と🌊カモメ",
                "output": {"width": 1280, "height": 720},
                "frames": 56,
                "quality": "draft",
                "seed": 3,
                "start_image": {"data": image, "media_type": "image/png"},
            },
        ).json()
        deadline = time.monotonic() + 60
        body: dict = {}
        while time.monotonic() < deadline:
            body = client.get(f"/v1/jobs/{job['id']}").json()
            if body["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.1)
        assert body["status"] == "succeeded", body
        assert body["result"]["output"] == {
            "media_type": "video/mp4",
            "width": 1280,
            "height": 720,
            "frames": 56,
            "fps": 24,
            "duration_sec": 2.333,
        }
        assert body["result"]["seed_used"] == 3
        assert body["result"]["details"]["generation"]["width"] == 832
        out = tmp_path / "out.mp4"
        out.write_bytes(client.get(f"/v1/jobs/{job['id']}/output").content)

    info = probe_video(out)
    assert (info.width, info.height, info.frames, info.has_audio) == (1280, 720, 56, False)
    assert info.fps == 24.0  # the file itself, not just the result JSON
    spec = json.loads(calls.read_text().splitlines()[0])
    prompt = next(s for s in spec["stages"] if s["id"] == "text-prompt")["config"]["text"]
    assert prompt == "波と🌊カモメ"  # UTF-8 kept intact for vpipe's parser
    assert not (settings.data_dir / "jobs" / job["id"] / "raw.mkv").exists()


@needs_ffmpeg
def test_stage_failure_marks_job_failed(settings: Settings, fake_mode) -> None:
    fake_mode("stage_fail")
    vpipe_bin, work_dir = settings.require_runtime()
    registry = build_registry(settings)
    queue = JobQueue(
        JobStore(settings.data_dir),
        registry,
        VpipeRunner(vpipe_bin, work_dir),
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
    )
    app = create_app(queue, registry, token=None, max_body_bytes=1 << 20)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        job = client.post(
            "/v1/workflows/minimax-h3-turbo-video/jobs",
            json={"prompt": "x", "output": {"width": 832, "height": 480}, "frames": 56},
        ).json()
        deadline = time.monotonic() + 30
        body: dict = {}
        while time.monotonic() < deadline:
            body = client.get(f"/v1/jobs/{job['id']}").json()
            if body["status"] == "failed":
                break
            time.sleep(0.1)
    assert body["error"]["code"] == "generation_failed"
    assert "generate-video" in body["error"]["message"]


def _numbered_clip(path: Path, frames: int) -> Path:
    """1344x768 at 24 fps with AAC audio; frame n carries n as 8 black/white blocks on top."""
    bits = "if(mod(floor(N/pow(2\\,floor(X/168)))\\,2)\\,235\\,16)"
    vf = (
        f"format=yuv444p,geq=lum='if(lt(Y\\,96)\\,{bits}\\,lum(X\\,Y))'"
        ":cb='if(lt(Y\\,96)\\,128\\,cb(X\\,Y))':cr='if(lt(Y\\,96)\\,128\\,cr(X\\,Y))'"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=1344x768:r=24",
            "-f",
            "lavfi",
            "-i",
            "sine=sample_rate=48000",
            "-vf",
            vf,
            "-frames:v",
            str(frames),
            "-c:v",
            "libx264",
            "-crf",
            "12",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


def _frame_numbers(path: Path, width: int, height: int) -> list[int]:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True,
        check=True,
    ).stdout
    band, size, numbers = height * 96 // 768 // 2, width * height, []
    for start in range(0, len(raw), size):
        row = raw[start + band * width : start + (band + 1) * width]
        cells = (row[k * width // 8 + width // 16] for k in range(8))
        numbers.append(sum(1 << k for k, value in enumerate(cells) if value > 128))
    return numbers


@needs_ffmpeg
def test_upscale_job_over_http_keeps_every_source_frame(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_VPIPE_CALLS", str(calls))
    vpipe_bin, work_dir = settings.require_runtime()
    install_models(work_dir)
    registry = build_registry(settings)
    queue = JobQueue(
        JobStore(settings.data_dir),
        registry,
        VpipeRunner(vpipe_bin, work_dir),
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
    )
    app = create_app(queue, registry, token=None, max_body_bytes=64 << 20, work_dir=work_dir)
    source = _numbered_clip(tmp_path / "take.mp4", 56)
    video = {"data": base64.b64encode(source.read_bytes()).decode(), "media_type": "video/mp4"}

    with TestClient(app, base_url="http://127.0.0.1") as client:
        job = client.post(
            "/v1/workflows/flashvsr-upscale/jobs",
            json={"source_video": video, "output": {"width": 1920, "height": 1080}},
            headers={"Idempotency-Key": "take-1-upscale"},
        ).json()
        deadline = time.monotonic() + 60
        body: dict = {}
        while time.monotonic() < deadline:
            body = client.get(f"/v1/jobs/{job['id']}").json()
            if body["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.1)
        assert body["status"] == "succeeded", body
        assert body["result"]["output"] == {
            "media_type": "video/mp4",
            "width": 1920,
            "height": 1080,
            "frames": 56,
            "fps": 24,
            "duration_sec": 2.333,
        }
        assert body["result"]["seed_used"] is None
        assert {"queue_seconds", "backend_seconds", "total_seconds"} <= body["timings"].keys()
        out = tmp_path / "out.mp4"
        out.write_bytes(client.get(f"/v1/jobs/{job['id']}/output").content)

    info = probe_video(out)
    assert (info.width, info.height, info.frames, info.fps, info.has_audio) == (
        1920,
        1080,
        56,
        24.0,
        True,
    )
    assert _frame_numbers(out, 1920, 1080) == list(range(56))  # no clone, none missing
    spec = json.loads(calls.read_text().splitlines()[0])
    gen = next(s for s in spec["stages"] if s["id"] == "generate-video")["config"]
    assert (gen["width"], gen["height"]) == (1920, 1152)
    job_dir = settings.data_dir / "jobs" / job["id"]
    assert not (job_dir / "raw.mkv").exists() and not (job_dir / "inputs").exists()
