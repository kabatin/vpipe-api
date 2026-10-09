import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import install_models
from tests.fakes import EchoWorkflow, FakeRunner
from vpipe_api.api.app import create_app
from vpipe_api.jobs.queue import JobQueue
from vpipe_api.jobs.store import JobStore
from vpipe_api.workflows.base import MediaTools, WorkflowRegistry
from vpipe_api.workflows.h3_video import H3VideoWorkflow

TOKEN = "test-token-0123456789-abcdefghijklmnop"


def make_client(
    tmp_path: Path, runner: FakeRunner, token: str | None = None, max_body: int = 1 << 20
) -> TestClient:
    registry = WorkflowRegistry([EchoWorkflow(), H3VideoWorkflow(MediaTools())])
    queue = JobQueue(
        JobStore(tmp_path), registry, runner, max_waiting=1, timeout_factor=2, retention_days=7
    )
    app = create_app(queue, registry, token=token, max_body_bytes=max_body)
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def client(tmp_path: Path, runner: FakeRunner) -> Iterator[TestClient]:
    with make_client(tmp_path, runner) as c:
        yield c
    runner.release.set()


def poll(client: TestClient, job_id: str, want: str, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/v1/jobs/{job_id}").json()
        if body["status"] == want:
            return body
        time.sleep(0.02)
    raise AssertionError(f"job never reached {want}")


def test_health_and_workflows(client: TestClient) -> None:
    assert client.get("/v1/health").json() == {
        "status": "ok",
        "version": "0.1.3",
        "running": 0,
        "waiting": 0,
        "max_waiting": 1,
        "outside_runs": 0,
    }
    flows = {w["id"]: w for w in client.get("/v1/workflows").json()["workflows"]}
    assert set(flows) == {"echo", "minimax-h3-turbo-video"}
    assert "frames" in flows["minimax-h3-turbo-video"]["params_schema"]["properties"]


def test_openapi_has_typed_submit_route(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    route = spec["paths"]["/v1/workflows/minimax-h3-turbo-video/jobs"]["post"]
    assert route["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "H3VideoParams"
    )
    assert "429" in route["responses"]


def test_full_job_flow(client: TestClient) -> None:
    accepted = client.post("/v1/workflows/echo/jobs", json={"text": "hi"})
    assert accepted.status_code == 202
    job = accepted.json()
    assert (job["workflow"], job["status"]) == ("echo", "queued")
    assert job["estimate_seconds"] > 0  # known from the submit on
    done = poll(client, job["id"], "succeeded")
    assert done["result"] == {"length": 2} and done["error"] is None
    assert {"queue_seconds", "backend_seconds", "total_seconds"} <= done["timings"].keys()
    assert done["estimate_seconds"] == job["estimate_seconds"]
    out = client.get(f"/v1/jobs/{job['id']}/output")
    assert out.status_code == 200 and out.text == "HI"
    assert out.headers["content-type"].startswith("text/plain")
    assert client.delete(f"/v1/jobs/{job['id']}").json()["error"]["code"] == "conflict"


def test_errors_use_the_envelope(client: TestClient) -> None:
    missing = client.get("/v1/jobs/job_nope")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert client.get("/v1/jobs/job_nope/output").status_code == 404
    assert client.delete("/v1/jobs/job_nope").status_code == 404
    unknown = client.post("/v1/workflows/nope/jobs", json={})
    assert unknown.status_code == 404
    bad = client.post(
        "/v1/workflows/minimax-h3-turbo-video/jobs",
        json={
            "prompt": "x",
            "output": {"width": 10, "height": 10},
            "start_image": {"data": "A" * 4000, "media_type": "image/png"},
        },
    )
    body = bad.json()["error"]
    assert bad.status_code == 422 and body["code"] == "invalid_params"
    assert "A" * 100 not in bad.text  # never echo inputs back
    assert all(set(d) == {"loc", "msg"} for d in body["details"])
    rejected = client.post("/v1/workflows/echo/jobs", json={"text": "x", "reject": True})
    assert rejected.status_code == 422 and "rejected" in rejected.json()["error"]["message"]


def test_busy_returns_429_with_retry_after(client: TestClient, runner: FakeRunner) -> None:
    runner.block = True
    first = client.post("/v1/workflows/echo/jobs", json={"text": "a"}).json()
    runner.started.wait(2)
    second = client.post("/v1/workflows/echo/jobs", json={"text": "b"}).json()
    assert client.get(f"/v1/jobs/{second['id']}").json()["queue_position"] == 1
    busy = client.post("/v1/workflows/echo/jobs", json={"text": "c"})
    assert busy.status_code == 429
    assert busy.json()["error"] == {
        "code": "busy",
        "message": "the GPU slot and the waiting queue are full",
        "retryable": True,
        "details": None,
    }
    assert int(busy.headers["retry-after"]) >= 30
    assert client.get(f"/v1/jobs/{first['id']}/output").status_code == 409
    assert client.delete(f"/v1/jobs/{second['id']}").json()["status"] == "canceled"
    assert client.delete(f"/v1/jobs/{first['id']}").json()["status"] == "canceled"


def test_bearer_auth_everywhere(tmp_path: Path, runner: FakeRunner) -> None:
    with make_client(tmp_path, runner, token=TOKEN) as c:
        for path in ("/v1/health", "/openapi.json", "/docs"):
            denied = c.get(path)
            assert denied.status_code == 401 and denied.headers["www-authenticate"] == "Bearer"
        assert c.get("/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = c.get("/v1/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200


def test_body_limits(tmp_path: Path, runner: FakeRunner) -> None:
    with make_client(tmp_path, runner, max_body=100) as c:
        big = c.post("/v1/workflows/echo/jobs", json={"text": "x" * 500})
        assert big.status_code == 413 and big.json()["error"]["code"] == "payload_too_large"
        chunked = c.post(
            "/v1/workflows/echo/jobs",
            content=iter([b'{"text":"x"}']),
            headers={"Content-Type": "application/json"},
        )
        assert chunked.status_code == 411


def test_missing_models_refuse_the_submit_before_the_body(
    tmp_path: Path, runner: FakeRunner
) -> None:
    registry = WorkflowRegistry([EchoWorkflow(), H3VideoWorkflow(MediaTools())])
    queue = JobQueue(
        JobStore(tmp_path), registry, runner, max_waiting=1, timeout_factor=2, retention_days=7
    )
    work_dir = tmp_path / "work"
    app = create_app(queue, registry, token=None, max_body_bytes=1 << 20, work_dir=work_dir)
    path = "/v1/workflows/minimax-h3-turbo-video/jobs"
    junk = {"content": b"not json", "headers": {"Content-Type": "application/json"}}
    with TestClient(app, base_url="http://127.0.0.1") as c:
        refused = c.post(path, **junk)
        assert refused.status_code == 409
        error = refused.json()["error"]
        assert (error["code"], error["retryable"]) == ("model_not_installed", False)
        assert "not downloaded" in error["message"]
        assert "vpipe-api setup models minimax-h3-turbo-video" in error["message"]
        assert str(tmp_path) not in error["message"]
        assert c.post("/v1/workflows/echo/jobs", json={"text": "hi"}).status_code == 202
        listed = [w["id"] for w in c.get("/v1/workflows").json()["workflows"]]
        assert "minimax-h3-turbo-video" in listed  # still advertised
        install_models(work_dir)
        assert c.post(path, **junk).status_code == 422  # past the gate, the body is read


def test_auth_is_checked_before_the_model_gate(tmp_path: Path, runner: FakeRunner) -> None:
    registry = WorkflowRegistry([H3VideoWorkflow(MediaTools())])
    queue = JobQueue(
        JobStore(tmp_path), registry, runner, max_waiting=1, timeout_factor=2, retention_days=7
    )
    app = create_app(
        queue, registry, token=TOKEN, max_body_bytes=1 << 20, work_dir=tmp_path / "work"
    )
    with TestClient(app, base_url="http://127.0.0.1") as c:
        refused = c.post("/v1/workflows/minimax-h3-turbo-video/jobs", json={})
        assert refused.status_code == 401  # install state is not shown to strangers


def test_health_reports_vpipe_runs_outside_the_server(tmp_path: Path, runner: FakeRunner) -> None:
    registry = WorkflowRegistry([EchoWorkflow()])
    queue = JobQueue(
        JobStore(tmp_path),
        registry,
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
        outside_runs=lambda: 1,
    )
    app = create_app(queue, registry, token=None, max_body_bytes=1 << 20)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        health = c.get("/v1/health").json()
    assert (health["running"], health["outside_runs"]) == (0, 1)

    queue_unknown = JobQueue(
        JobStore(tmp_path / "u"),
        registry,
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
        outside_runs=lambda: None,  # ps could not be read
    )
    app = create_app(queue_unknown, registry, token=None, max_body_bytes=1 << 20)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.get("/v1/health").json()["outside_runs"] is None
