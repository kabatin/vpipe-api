"""Coordination with other GPU servers: the health probe (against a real local HTTP server),
the double check ``settle_seconds`` apart, and the queue's ``waiting_for`` and
``waiting_for_peer``."""

from __future__ import annotations

import json
import socket
import threading
import time
import traceback
from collections.abc import Iterator
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.fakes import EchoParams, EchoWorkflow, FakeRunner
from vpipe_api import __version__
from vpipe_api.api.app import create_app
from vpipe_api.jobs.coordination import (
    Coordinator,
    PeerHealth,
    PeerUnreachableError,
    probe_health,
)
from vpipe_api.jobs.models import JobStatus
from vpipe_api.jobs.queue import JobQueue
from vpipe_api.jobs.store import JobStore
from vpipe_api.settings import PeerServer, SettingsError, load_settings
from vpipe_api.workflows.base import WorkflowRegistry

PEER_TOKEN = "peer-token-0123456789"


class FakePeer:
    """An ace-step-api-like ``/v1/health`` on 127.0.0.1; ``state`` is served as JSON."""

    def __init__(self) -> None:
        self.state: dict = {
            "status": "ok",
            "version": "0.1.0",
            "running": 0,
            "waiting": 0,
            "max_waiting": 2,
            "waiting_for_peer": 0,
        }
        self.status = 200
        self.headers_seen: list[dict[str, str]] = []
        self.paths_seen: list[str] = []
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                peer.headers_seen.append(dict(self.headers))
                peer.paths_seen.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/steal")
                    self.end_headers()
                    return
                if self.headers.get("Authorization") != f"Bearer {PEER_TOKEN}":
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.dumps(peer.state).encode()
                self.send_response(peer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def peer(self, path: str = "/v1/health", token: str | None = PEER_TOKEN) -> PeerServer:
        return PeerServer.model_validate(
            {"name": "ace-step-api", "health_url": self.url + path, "token": token}
        )


@pytest.fixture
def fake_peer() -> Iterator[FakePeer]:
    peer = FakePeer()
    yield peer
    peer.server.shutdown()
    peer.server.server_close()


# -- the probe -------------------------------------------------------------------------


def test_probe_reads_a_peer_health(fake_peer: FakePeer) -> None:
    health = probe_health(fake_peer.peer())
    assert health.idle and (health.running, health.waiting, health.outside_runs) == (0, 0, None)
    assert fake_peer.headers_seen[0]["Authorization"] == f"Bearer {PEER_TOKEN}"
    fake_peer.state |= {"running": 1}
    assert not probe_health(fake_peer.peer()).idle
    fake_peer.state |= {"running": 0, "waiting": 0, "outside_runs": 2}
    assert not probe_health(fake_peer.peer()).idle
    fake_peer.state |= {"outside_runs": 0}
    assert probe_health(fake_peer.peer()).idle
    # a peer that is itself waiting for another server yields: it counts as idle
    fake_peer.state |= {"waiting_for_peer": 2}
    assert probe_health(fake_peer.peer()).idle


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"token": None}, "HTTP 401"),
        ({"path": "/redirect"}, "HTTP 302"),
    ],
)
def test_probe_failures_are_unreachable(fake_peer: FakePeer, change: dict, needle: str) -> None:
    peer = fake_peer.peer(**change)
    with pytest.raises(PeerUnreachableError, match=needle) as info:
        probe_health(peer)
    assert PEER_TOKEN not in str(info.value)
    # a redirect is never followed, so the token never travels anywhere else
    assert len(fake_peer.headers_seen) == 1
    assert fake_peer.paths_seen == [change.get("path", "/v1/health")]


def test_probe_of_a_bad_body_or_dead_server(fake_peer: FakePeer) -> None:
    fake_peer.state = {"status": "ok"}
    with pytest.raises(PeerUnreachableError, match="unexpected health response"):
        probe_health(fake_peer.peer())
    fake_peer.status = 503
    with pytest.raises(PeerUnreachableError, match="HTTP 503"):
        probe_health(fake_peer.peer())
    dead = PeerServer(name="gone", health_url="http://127.0.0.1:1/v1/health")
    with pytest.raises(PeerUnreachableError):
        probe_health(dead, timeout_s=1)


class RawServer:
    """A TCP service on 127.0.0.1 that answers every connection with ``reply`` and hangs up
    (e.g. an SSH banner where an HTTP server was expected)."""

    def __init__(self, reply: bytes) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.reply = reply
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            with conn:
                conn.recv(65536)
                conn.sendall(self.reply)


@pytest.mark.parametrize(
    "reply",
    [
        b"SSH-2.0-OpenSSH_9.9\r\n",  # not HTTP at all: BadStatusLine
        b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\n{"running"',  # cut off
    ],
)
def test_a_peer_that_does_not_speak_http_is_unreachable(reply: bytes) -> None:
    raw = RawServer(reply)
    try:
        peer = PeerServer(name="odd", health_url=f"http://127.0.0.1:{raw.port}/v1/health")
        with pytest.raises(PeerUnreachableError):
            probe_health(peer, timeout_s=2)
    finally:
        raw.sock.close()


def test_the_probe_ignores_proxy_settings(
    fake_peer: FakePeer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy from the environment (or the system settings) must not see the peer token."""
    sink = RawServer(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
    try:
        for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{sink.port}")
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        assert probe_health(fake_peer.peer()).idle
        assert sink.connections == 0
    finally:
        sink.sock.close()


# -- settings ----------------------------------------------------------------------------


def test_coordinate_with_from_the_config_file(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        "settle_seconds = 10\n"
        "[[coordinate_with]]\n"
        'name = "ace-step-api"\nhealth_url = "http://127.0.0.1:8768/v1/health"\n'
        f'token = "{PEER_TOKEN}"\n'
        "[[coordinate_with]]\n"
        'name = "klein-api"\nhealth_url = "http://127.0.0.1:8767/v1/health"\n'
    )
    s = load_settings(env={}, config_path=cfg)
    assert [p.name for p in s.coordinate_with] == ["ace-step-api", "klein-api"]
    assert s.settle_seconds == 10.0 and s.coordinate_with[1].token is None
    assert s.coordination_poll_seconds == 5.0
    assert PEER_TOKEN not in repr(s)


def test_defaults_coordinate_with_nothing(tmp_path: Path) -> None:
    s = load_settings(env={}, config_path=tmp_path / "absent.toml")
    assert s.coordinate_with == () and s.settle_seconds == 30.0


def test_settle_and_poll_from_the_environment(tmp_path: Path) -> None:
    s = load_settings(
        env={"VPIPE_API_SETTLE_SECONDS": "12", "VPIPE_API_COORDINATION_POLL_SECONDS": "2"},
        config_path=tmp_path / "absent.toml",
    )
    assert (s.settle_seconds, s.coordination_poll_seconds) == (12.0, 2.0)


@pytest.mark.parametrize(
    ("entry", "needle"),
    [
        ('name = "x"\nhealth_url = "file:///etc/passwd"', "http"),
        ('name = "x"\nhealth_url = "http://user:pw@host/v1/health"', "credentials"),
        ('name = "bad name"\nhealth_url = "http://h/v1/health"', "name"),
        ('name = "x"\nhealth_url = "http://h/v1/health"\ntoken = "has space"', "token"),
        ('name = "x"\nhealth_url = "http://h/"\nextra = 1', "extra"),
        ('name = "x"\nhealth_url = "http://127.0.0.1:abc/v1/health"', "port"),
        ('name = "x"\nhealth_url = "http://127.0.0.1:8768/v1/he alth"', "space"),
        ('name = "x"\nhealth_url = "http://127.0.0.1:8768/v1/health?token=abc"', "query"),
    ],
)
def test_bad_peers_are_refused(tmp_path: Path, entry: str, needle: str) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"[[coordinate_with]]\n{entry}\n")
    with pytest.raises(SettingsError, match=needle):
        load_settings(env={}, config_path=cfg)


def test_a_bad_peer_token_is_not_echoed(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[[coordinate_with]]\nname = "x"\nhealth_url = "http://h/v1/health"\n'
        'token = "secret with space"\n'
    )
    with pytest.raises(SettingsError) as info:
        load_settings(env={}, config_path=cfg)
    assert "secret with space" not in str(info.value)
    # nor in what a traceback would print
    assert "secret with space" not in "".join(traceback.format_exception(info.value))


def test_peer_names_must_be_unique(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    entry = '[[coordinate_with]]\nname = "x"\nhealth_url = "http://h/v1/health"\n'
    cfg.write_text(entry * 2)
    with pytest.raises(SettingsError, match="unique"):
        load_settings(env={}, config_path=cfg)


# -- the settle rule ---------------------------------------------------------------------


class ScriptedProbe:
    """Answers from a list of states per call (the last one repeats)."""

    def __init__(self, *states: PeerHealth | Exception) -> None:
        self.states = list(states)
        self.calls = 0

    def __call__(self, peer: PeerServer) -> PeerHealth:
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        if isinstance(state, Exception):
            raise state
        return state


class FakeClock:
    """Monotonic time that only moves when the coordinator sleeps (or the test says so)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def ignore(_peer: str) -> None:
    pass


IDLE = PeerHealth(running=0, waiting=0)
BUSY = PeerHealth(running=1, waiting=0)
PEER = PeerServer(name="ace-step-api", health_url="http://127.0.0.1:8768/v1/health")


def coordinator_with(
    probe, clock: FakeClock, *, peers=(PEER,), settle: float = 30, poll: float = 5
) -> Coordinator:
    return Coordinator(peers, settle_seconds=settle, poll_seconds=poll, probe=probe, clock=clock)


def run_wait(
    coordinator: Coordinator, clock: FakeClock
) -> tuple[bool, list[float], list[str], float]:
    """Returns (started, sleeps, waiting_for values, clock time when it returned)."""
    sleeps: list[float] = []
    waiting: list[str] = []

    def sleep(seconds: float) -> bool:
        sleeps.append(round(seconds, 3))
        clock.now += seconds
        return True

    ok = coordinator.wait_until_idle("job_x", sleep=sleep, on_waiting=waiting.append)
    return ok, sleeps, waiting, clock.now


def test_first_ever_idle_check_starts_immediately() -> None:
    clock, probe = FakeClock(), ScriptedProbe(IDLE)
    ok, sleeps, waiting, _ = run_wait(coordinator_with(probe, clock), clock)
    assert ok and probe.calls == 1 and sleeps == [] and waiting == []


def test_idle_long_after_the_last_busy_sighting_starts_immediately() -> None:
    clock, probe = FakeClock(), ScriptedProbe(BUSY, IDLE)
    coordinator = coordinator_with(probe, clock)
    assert run_wait(coordinator, clock)[0]  # first job: busy, then settled
    clock.now += 31  # next job arrives more than settle_seconds after the last busy sighting
    calls = probe.calls
    ok, sleeps, waiting, _ = run_wait(coordinator, clock)
    assert ok and probe.calls == calls + 1 and sleeps == [] and waiting == []


def test_busy_then_idle_waits_settle_seconds_after_the_busy_sighting() -> None:
    clock, probe = FakeClock(), ScriptedProbe(BUSY, IDLE)
    busy_at = clock.now
    ok, sleeps, waiting, ended = run_wait(coordinator_with(probe, clock, poll=5), clock)
    # busy at t, idle at t+5 (too soon), idle again at t+30 -> start
    assert ok and probe.calls == 3 and sleeps == [5.0, 25.0]
    assert ended == busy_at + 30
    assert waiting == ["ace-step-api", "ace-step-api"]


def test_a_recent_busy_sighting_from_an_earlier_wait_still_counts() -> None:
    clock, probe = FakeClock(), ScriptedProbe(BUSY)
    coordinator = coordinator_with(probe, clock, poll=5)
    # job A sees the peer busy at t=1000 and is canceled while waiting
    abandoned = coordinator.wait_until_idle("a", sleep=lambda _s: False, on_waiting=ignore)
    assert not abandoned and coordinator.last_busy == 1000.0
    # job B checks at t=1010: the peer is idle, but was busy 10 s ago -> wait until t=1030
    clock.now = 1010.0
    probe.states, probe.calls = [IDLE], 0
    ok, sleeps, waiting, ended = run_wait(coordinator, clock)
    assert ok and probe.calls == 2 and sleeps == [20.0] and ended == 1030.0
    assert waiting == ["ace-step-api"]


def test_busy_again_during_the_settle_wait_moves_the_window() -> None:
    clock, probe = FakeClock(), ScriptedProbe(BUSY, IDLE, BUSY, IDLE, IDLE)
    ok, sleeps, _, ended = run_wait(coordinator_with(probe, clock, poll=5), clock)
    # busy t, idle t+5, busy t+30, idle t+35, idle t+60 -> start
    assert ok and probe.calls == 5 and sleeps == [5.0, 25.0, 5.0, 25.0]
    assert ended == 1060.0


def test_zero_settle_starts_on_the_first_idle_check() -> None:
    clock, probe = FakeClock(), ScriptedProbe(BUSY, IDLE)
    ok, sleeps, _, _ = run_wait(coordinator_with(probe, clock, settle=0, poll=5), clock)
    assert ok and sleeps == [5.0]


def test_unreachable_counts_as_idle(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    probe = ScriptedProbe(PeerUnreachableError("ConnectionRefusedError"))
    ok, sleeps, _, _ = run_wait(coordinator_with(probe, clock), clock)
    assert ok and probe.calls == 1 and sleeps == []
    assert "ace-step-api" in caplog.text and "ConnectionRefusedError" in caplog.text


def test_an_unexpected_probe_error_counts_as_idle(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    probe = ScriptedProbe(RuntimeError("a bug in the probe"))
    ok, sleeps, _, _ = run_wait(coordinator_with(probe, clock), clock)
    assert ok and probe.calls == 1 and sleeps == []
    assert "ace-step-api" in caplog.text


def test_every_peer_must_be_idle() -> None:
    klein = PeerServer(name="klein-api", health_url="http://127.0.0.1:8767/v1/health")
    states = {"ace-step-api": [IDLE], "klein-api": [BUSY, IDLE]}

    def probe(peer: PeerServer) -> PeerHealth:
        queue = states[peer.name]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    clock = FakeClock()
    ok, sleeps, waiting, _ = run_wait(
        coordinator_with(probe, clock, peers=(PEER, klein), settle=10, poll=2), clock
    )
    # the settle wait names the server that was busy, not just the first one listed
    assert ok and waiting == ["klein-api", "klein-api"] and sleeps == [2.0, 8.0]


def test_an_abandoned_wait_returns_false() -> None:
    coordinator = Coordinator([PEER], settle_seconds=30, probe=ScriptedProbe(BUSY))
    assert not coordinator.wait_until_idle("j", sleep=lambda _s: False, on_waiting=lambda _n: None)
    settling = Coordinator([PEER], settle_seconds=30, probe=ScriptedProbe(BUSY, IDLE))
    assert not settling.wait_until_idle(
        "j", sleep=lambda s: s < 10, on_waiting=lambda _n: None
    )  # poll sleep ok, settle sleep abandoned
    assert Coordinator([]).wait_until_idle("j", sleep=lambda _s: False, on_waiting=ignore)


# -- through the queue -------------------------------------------------------------------


class Switch:
    """A probe the test flips between busy and idle; remembers the wall time of its last busy
    answer."""

    def __init__(self) -> None:
        self.busy = True
        self.calls = 0
        self.last_busy_at: float | None = None

    def __call__(self, peer: PeerServer) -> PeerHealth:
        self.calls += 1
        if self.busy:
            self.last_busy_at = time.time()
            return BUSY
        return IDLE


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def make_queue(
    tmp_path: Path, runner: FakeRunner, coordinator: Coordinator, outside=lambda: 0
) -> JobQueue:
    return JobQueue(
        JobStore(tmp_path),
        WorkflowRegistry([EchoWorkflow()]),
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
        outside_runs=outside,
        outside_poll_s=0.05,
        coordinator=coordinator,
    )


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def switch() -> Switch:
    return Switch()


@pytest.fixture
def client(tmp_path: Path, runner: FakeRunner, switch: Switch) -> Iterator[TestClient]:
    coordinator = Coordinator([PEER], settle_seconds=0.4, poll_seconds=0.05, probe=switch)
    queue = make_queue(tmp_path, runner, coordinator)
    registry = WorkflowRegistry([EchoWorkflow()])
    app = create_app(queue, registry, token=None, max_body_bytes=1 << 20)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c
    runner.release.set()


def test_a_job_waits_queued_while_another_gpu_server_is_busy(
    client: TestClient, runner: FakeRunner, switch: Switch
) -> None:
    job = client.post("/v1/workflows/echo/jobs", json={"text": "a"}).json()
    wait_for(lambda: client.get(f"/v1/jobs/{job['id']}").json()["waiting_for"] == "ace-step-api")
    body = client.get(f"/v1/jobs/{job['id']}").json()
    assert (body["status"], body["queue_position"], body["progress"]) == ("queued", 1, None)
    # waiting for another server is not running: the slot is not claimed, and the queue is
    # reported as waiting_for_peer so a server that waits for this one is not deadlocked
    assert client.get("/v1/health").json() == {
        "status": "ok",
        "version": __version__,
        "running": 0,
        "waiting": 0,
        "max_waiting": 1,
        "outside_runs": 0,
        "waiting_for_peer": 1,
    }
    time.sleep(0.3)
    assert runner.calls == 0
    switch.busy = False
    wait_for(lambda: client.get(f"/v1/jobs/{job['id']}").json()["status"] == "succeeded")
    done = client.get(f"/v1/jobs/{job['id']}").json()
    assert done["waiting_for"] is None
    # it started no sooner than settle_seconds (0.4) after the last busy answer
    started = datetime.fromisoformat(done["started_at"]).timestamp()
    assert switch.last_busy_at is not None and started >= switch.last_busy_at + 0.35
    assert client.get("/v1/health").json()["waiting_for_peer"] == 0


def test_the_queue_is_still_full_while_waiting_for_a_peer(client: TestClient) -> None:
    first = client.post("/v1/workflows/echo/jobs", json={"text": "a"}).json()
    wait_for(lambda: client.get(f"/v1/jobs/{first['id']}").json()["waiting_for"] is not None)
    second = client.post("/v1/workflows/echo/jobs", json={"text": "b"})
    assert second.status_code == 202
    assert client.get(f"/v1/jobs/{second.json()['id']}").json()["waiting_for"] is None
    assert client.get("/v1/health").json()["waiting_for_peer"] == 2
    # waiting: 0 is only what other servers see; capacity still counts every queued job
    assert client.post("/v1/workflows/echo/jobs", json={"text": "c"}).status_code == 429


def test_canceling_a_coordinating_job_is_immediate(
    client: TestClient, runner: FakeRunner, switch: Switch
) -> None:
    job = client.post("/v1/workflows/echo/jobs", json={"text": "a"}).json()
    wait_for(lambda: client.get(f"/v1/jobs/{job['id']}").json()["waiting_for"] is not None)
    canceled = client.delete(f"/v1/jobs/{job['id']}")
    assert canceled.status_code == 200 and canceled.json()["status"] == "canceled"
    assert canceled.json()["waiting_for"] is None
    # the next job is the head now and goes through once the peer is idle
    switch.busy = False
    second = client.post("/v1/workflows/echo/jobs", json={"text": "b"}).json()
    wait_for(lambda: client.get(f"/v1/jobs/{second['id']}").json()["status"] == "succeeded")
    assert runner.calls == 1


def test_stopping_the_server_ends_a_coordination_wait(
    tmp_path: Path, runner: FakeRunner, switch: Switch
) -> None:
    coordinator = Coordinator([PEER], settle_seconds=60, poll_seconds=60, probe=switch)
    q = make_queue(tmp_path, runner, coordinator)
    q.start()
    record, _ = q.submit(EchoWorkflow(), EchoParams(text="a"))
    wait_for(lambda: q.view(record.id).waiting_for is not None)
    started = time.monotonic()
    q.stop(timeout_s=5)
    assert time.monotonic() - started < 2
    assert q.view(record.id).record.status is JobStatus.QUEUED  # runs again after a restart
    assert runner.calls == 0


def test_without_peers_there_is_no_coordination(tmp_path: Path, runner: FakeRunner) -> None:
    probe = ScriptedProbe(BUSY)
    q = make_queue(tmp_path, runner, Coordinator([], probe=probe))
    q.start()
    try:
        record, _ = q.submit(EchoWorkflow(), EchoParams(text="a"))
        wait_for(lambda: q.view(record.id).record.status is JobStatus.SUCCEEDED)
        assert probe.calls == 0
    finally:
        q.stop(timeout_s=5)


def test_idle_peers_do_not_delay_a_job(tmp_path: Path, runner: FakeRunner) -> None:
    """With settle_seconds = 60 and peers that were never busy, the job starts at once."""
    probe = ScriptedProbe(IDLE)
    coordinator = Coordinator([PEER], settle_seconds=60, poll_seconds=60, probe=probe)
    q = make_queue(tmp_path, runner, coordinator)
    q.start()
    try:
        record, _ = q.submit(EchoWorkflow(), EchoParams(text="a"))
        wait_for(lambda: q.view(record.id).record.status is JobStatus.SUCCEEDED)
        assert probe.calls == 1
    finally:
        q.stop(timeout_s=5)


# -- next to vpipe runs outside the server --------------------------------------------------


def test_a_job_held_for_an_outside_run_is_waiting_not_waiting_for_peer(
    tmp_path: Path, runner: FakeRunner
) -> None:
    """Holding for a vpipe run outside the server stays as it was: other servers see the
    queue as ``waiting`` (vpipe-api is about to use the GPU), and nobody is asked."""
    outside = [1]
    probe = ScriptedProbe(IDLE)
    coordinator = Coordinator([PEER], settle_seconds=0.2, poll_seconds=0.05, probe=probe)
    q = make_queue(tmp_path, runner, coordinator, outside=lambda: outside[0])
    q.start()
    try:
        record, _ = q.submit(EchoWorkflow(), EchoParams(text="held"))
        time.sleep(0.3)
        assert runner.calls == 0 and probe.calls == 0
        assert q.counts() == (0, 1, 0)
        assert q.view(record.id).waiting_for is None
        outside[0] = 0
        wait_for(lambda: q.view(record.id).record.status is JobStatus.SUCCEEDED)
    finally:
        q.stop(timeout_s=5)


def test_an_outside_run_that_begins_during_coordination_still_holds_the_job(
    tmp_path: Path, runner: FakeRunner, switch: Switch
) -> None:
    outside = [0]
    coordinator = Coordinator([PEER], settle_seconds=0.2, poll_seconds=0.05, probe=switch)
    q = make_queue(tmp_path, runner, coordinator, outside=lambda: outside[0])
    q.start()
    try:
        record, _ = q.submit(EchoWorkflow(), EchoParams(text="x"))
        wait_for(lambda: q.view(record.id).waiting_for == "ace-step-api")
        assert q.counts() == (0, 0, 1)
        outside[0] = 1  # an experiment starts while the job waits for the peer
        switch.busy = False
        wait_for(lambda: q.view(record.id).waiting_for is None)
        time.sleep(0.5)  # well past settle_seconds: the peer is idle, the outside run is not
        assert runner.calls == 0
        assert q.counts() == (0, 1, 0)
        outside[0] = 0
        wait_for(lambda: q.view(record.id).record.status is JobStatus.SUCCEEDED)
    finally:
        q.stop(timeout_s=5)


def test_the_worker_survives_an_error_while_taking_the_next_job(
    tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead worker would leave every job queued forever while /v1/health still says ok."""
    coordinator = Coordinator(
        [PEER], settle_seconds=0, poll_seconds=0.05, probe=ScriptedProbe(IDLE)
    )
    q = make_queue(tmp_path, runner, coordinator)
    real = q._coordinate
    calls = []

    def flaky(job_id: str) -> bool:
        calls.append(job_id)
        if len(calls) == 1:
            raise RuntimeError("unexpected")
        return real(job_id)

    monkeypatch.setattr(q, "_coordinate", flaky)
    monkeypatch.setattr("vpipe_api.jobs.queue.WORKER_BACKOFF_S", 0.05)
    q.start()
    try:
        record, _ = q.submit(EchoWorkflow(), EchoParams(text="a"))
        wait_for(lambda: q.view(record.id).record.status is JobStatus.SUCCEEDED)
        assert len(calls) == 2
    finally:
        q.stop(timeout_s=5)
