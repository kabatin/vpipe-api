"""The real H3 workflow, real runner and real ffmpeg, against the fake vpipe."""

import base64
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_png, needs_ffmpeg
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
