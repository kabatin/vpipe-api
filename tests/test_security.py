"""Hardening: loopback-only mode, token rules, idempotency, busy gate, cleanup, inputs."""

import io
import os
import struct
import time
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.fakes import EchoParams, EchoWorkflow, FakeRunner
from vpipe_api.api.app import create_app
from vpipe_api.api.security import host_name
from vpipe_api.doctor import Level, check_config_file
from vpipe_api.jobs.queue import JobQueue
from vpipe_api.jobs.store import JobStore
from vpipe_api.media import MediaError, normalize_image
from vpipe_api.settings import SettingsError, load_settings
from vpipe_api.setup.vpipe_build import KNOWN_COMMITS, SetupError, binary_path, build_vpipe
from vpipe_api.workflows.base import WorkflowRegistry

TOKEN = "t" * 40


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def queue(tmp_path: Path, runner: FakeRunner) -> JobQueue:
    return JobQueue(
        JobStore(tmp_path),
        WorkflowRegistry([EchoWorkflow()]),
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
    )


@pytest.fixture
def client(queue: JobQueue, runner: FakeRunner) -> Iterator[TestClient]:
    app = create_app(queue, WorkflowRegistry([EchoWorkflow()]), token=None, max_body_bytes=1 << 20)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c
    runner.release.set()


# -- loopback-only mode (no token) -------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "LOCALHOST"])
def test_loopback_hosts_are_served(client: TestClient, host: str) -> None:
    assert client.get("/v1/health", headers={"Host": host}).status_code == 200


def test_rebinding_host_is_refused(client: TestClient) -> None:
    denied = client.get("/v1/health", headers={"Host": "attacker.example"})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "forbidden_host"


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP"])
def test_proxied_requests_need_a_token(client: TestClient, header: str) -> None:
    denied = client.get("/v1/health", headers={header: "203.0.113.9"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "proxy_requires_token"


def test_host_name_parsing() -> None:
    assert host_name("[::1]:8765") == "::1"
    assert host_name("localhost:1") == "localhost"
    assert host_name("127.0.0.1") == "127.0.0.1"
    assert host_name("[broken") == "[broken"


def test_body_without_content_type_is_not_json(client: TestClient) -> None:
    # a cross-site "simple" POST (no preflight) must not be accepted as a job
    sent = client.post("/v1/workflows/echo/jobs", content=b'{"text":"x"}')
    assert sent.status_code == 422


# -- tokens ----------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["short", "has space " * 5, "ü" * 40])
def test_weak_tokens_are_rejected(tmp_path: Path, token: str) -> None:
    with pytest.raises(SettingsError, match="printable ASCII"):
        load_settings(env={"VPIPE_API_TOKEN": token}, config_path=tmp_path / "x")


def test_bearer_scheme_is_case_insensitive(queue: JobQueue) -> None:
    app = create_app(queue, WorkflowRegistry([EchoWorkflow()]), token=TOKEN, max_body_bytes=1024)
    with TestClient(app) as c:
        assert c.get("/v1/health", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200
        assert c.get("/v1/health", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
        # with a token, any Host is fine (the LAN case)
        ok = c.get("/v1/health", headers={"Authorization": f"Bearer {TOKEN}", "Host": "mac.lan"})
        assert ok.status_code == 200


# -- idempotency and the busy gate -------------------------------------------------------


def test_idempotent_resubmission_over_http(client: TestClient, runner: FakeRunner) -> None:
    runner.block = True
    headers = {"Idempotency-Key": "gen-42"}
    first = client.post("/v1/workflows/echo/jobs", json={"text": "a"}, headers=headers)
    assert first.status_code == 202
    runner.started.wait(2)
    again = client.post("/v1/workflows/echo/jobs", json={"text": "a"}, headers=headers)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    clash = client.post("/v1/workflows/echo/jobs", json={"text": "b"}, headers=headers)
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "idempotency_conflict"
    bad = client.post(
        "/v1/workflows/echo/jobs",
        json={"text": "a"},
        headers={"Idempotency-Key": "no spaces allowed"},
    )
    assert bad.status_code == 422


def test_busy_gate_answers_before_reading_the_body(client: TestClient, runner: FakeRunner) -> None:
    runner.block = True
    client.post("/v1/workflows/echo/jobs", json={"text": "a"}, headers={"Idempotency-Key": "k1"})
    runner.started.wait(2)
    client.post("/v1/workflows/echo/jobs", json={"text": "b"})
    # the body is not even JSON: only a gate in front of parsing can answer 429 here
    busy = client.post(
        "/v1/workflows/echo/jobs", content=b"not json", headers={"Content-Type": "application/json"}
    )
    assert busy.status_code == 429 and "retry-after" in busy.headers
    # a key that already has a job still gets that job back
    known = client.post(
        "/v1/workflows/echo/jobs", json={"text": "a"}, headers={"Idempotency-Key": "k1"}
    )
    assert known.status_code == 200


# -- ids, cleanup ------------------------------------------------------------------------


@pytest.mark.parametrize("job_id", ["job_%00", "job_" + "A" * 300, "..", "job_abc"])
def test_malformed_ids_are_404(client: TestClient, job_id: str) -> None:
    assert client.get(f"/v1/jobs/{job_id}").status_code == 404


def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


def test_inputs_are_dropped_after_cancel_and_finish(
    queue: JobQueue, runner: FakeRunner, tmp_path: Path
) -> None:
    queue.start()
    try:
        runner.block = True
        running, _ = queue.submit(EchoWorkflow(), EchoParams(text="a"))
        runner.started.wait(2)
        waiting, _ = queue.submit(EchoWorkflow(), EchoParams(text="b"))
        for job in (running, waiting):
            (tmp_path / "jobs" / job.id / "inputs").mkdir()
        queue.cancel(waiting.id)
        assert not (tmp_path / "jobs" / waiting.id / "inputs").exists()
        runner.release.set()
        _wait(lambda: queue.view(running.id).record.status.finished)
        _wait(lambda: not (tmp_path / "jobs" / running.id / "inputs").exists())
        assert (tmp_path / "jobs" / running.id / "job.json").is_file()
    finally:
        queue.stop(timeout_s=5)


def test_internal_errors_do_not_leak(queue: JobQueue, runner: FakeRunner) -> None:
    runner.mode = "crash"
    queue.start()
    try:
        job, _ = queue.submit(EchoWorkflow(), EchoParams(text="x"))
        _wait(lambda: queue.view(job.id).record.status.finished)
        error = queue.view(job.id).record.error
        assert error is not None and "exploded" not in error.message
    finally:
        queue.stop(timeout_s=5)


# -- images --------------------------------------------------------------------------------


def _png_with_huge_ztxt() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="PNG")
    data = buf.getvalue()
    payload = b"k\x00\x00" + zlib.compress(b"x" * (70 * 1024 * 1024))
    chunk = struct.pack(">I", len(payload)) + b"zTXt" + payload
    chunk += struct.pack(">I", zlib.crc32(b"zTXt" + payload) & 0xFFFFFFFF)
    return data[:33] + chunk + data[33:]  # after IHDR


def test_hostile_png_chunk_is_a_media_error() -> None:
    with pytest.raises(MediaError, match="cannot decode image"):
        normalize_image(_png_with_huge_ztxt(), "image/png")


def test_decode_errors_hide_object_reprs() -> None:
    with pytest.raises(MediaError) as info:
        normalize_image(b"garbage", "image/png")
    assert "0x" not in str(info.value)


# -- config file, build pinning ---------------------------------------------------------------


def test_config_file_permissions(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'token = "{TOKEN}"\n')
    os.chmod(cfg, 0o644)
    assert check_config_file(cfg).level is Level.WARN
    os.chmod(cfg, 0o600)
    assert check_config_file(cfg).level is Level.OK
    assert check_config_file(None).level is Level.OK


def test_moved_tag_is_refused(tmp_path: Path) -> None:
    def run(cmd):
        return 0

    with pytest.raises(SetupError, match="was moved"):
        build_vpipe(
            tmp_path / "v",
            run=run,
            has_tool=lambda _: True,
            metal_available=lambda: True,
            head_commit=lambda _: "deadbeef",
        )


def test_untested_tag_warns(tmp_path: Path) -> None:
    src = tmp_path / "v"
    messages: list[str] = []

    def run(cmd):
        if cmd[:2] == ["cmake", "--build"]:
            binary_path(src).parent.mkdir(parents=True)
            binary_path(src).write_text("")
        return 0

    build_vpipe(
        src,
        tag="v9.9.9",
        run=run,
        emit=messages.append,
        has_tool=lambda _: True,
        metal_available=lambda: True,
        head_commit=lambda _: "abc123",
    )
    assert any("not a tested tag" in m for m in messages)
    assert "v0.1.80" in KNOWN_COMMITS
