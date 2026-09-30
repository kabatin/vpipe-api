import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fakes import EchoParams, EchoWorkflow, FakeRunner
from vpipe_api.jobs.models import JobRecord, JobStatus
from vpipe_api.jobs.queue import (
    IdempotencyConflictError,
    JobConflictError,
    JobNotFoundError,
    JobQueue,
    QueueFullError,
    _phase_progress,
    redact,
)
from vpipe_api.jobs.store import JobStore
from vpipe_api.workflows.base import InvalidParamsError, WorkflowRegistry


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def queue(tmp_path: Path, runner: FakeRunner) -> Iterator[JobQueue]:
    q = JobQueue(
        JobStore(tmp_path),
        WorkflowRegistry([EchoWorkflow()]),
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
    )
    q.start()
    yield q
    runner.release.set()
    q.stop(timeout_s=5)


def status(queue: JobQueue, job_id: str) -> JobStatus:
    return queue.view(job_id).record.status


def test_job_runs_to_success(queue: JobQueue) -> None:
    record, _ = queue.submit(EchoWorkflow(), EchoParams(text="hello"))
    wait_for(lambda: status(queue, record.id) is JobStatus.SUCCEEDED)
    view = queue.view(record.id)
    assert view.progress == 1.0 and view.record.result == {"length": 5}
    assert queue.output_path(record.id).read_text() == "HELLO"


@pytest.mark.parametrize(
    ("mode", "code"), [("fail", "generation_failed"), ("timeout", "timeout"), ("crash", "internal")]
)
def test_failures_are_recorded(queue: JobQueue, runner: FakeRunner, mode: str, code: str) -> None:
    runner.mode = mode
    record, _ = queue.submit(EchoWorkflow(), EchoParams(text="x"))
    wait_for(lambda: status(queue, record.id) is JobStatus.FAILED)
    error = queue.view(record.id).record.error
    assert error is not None and error.code == code and error.retryable


def test_workflow_failure_keeps_its_code(queue: JobQueue) -> None:
    record, _ = queue.submit(EchoWorkflow(), EchoParams(text="x", fail_finalize=True))
    wait_for(lambda: status(queue, record.id) is JobStatus.FAILED)
    error = queue.view(record.id).record.error
    assert error is not None and (error.code, error.retryable) == ("postprocess_failed", False)
    with pytest.raises(JobConflictError):
        queue.output_path(record.id)


def test_capacity_busy_and_positions(queue: JobQueue, runner: FakeRunner) -> None:
    runner.block = True
    first, _ = queue.submit(EchoWorkflow(), EchoParams(text="a"))
    runner.started.wait(2)
    second, _ = queue.submit(EchoWorkflow(), EchoParams(text="b"))
    assert queue.counts() == (1, 1)
    assert queue.view(second.id).queue_position == 1
    assert queue.view(first.id).progress == pytest.approx(0.945)
    with pytest.raises(QueueFullError) as busy:
        queue.submit(EchoWorkflow(), EchoParams(text="c"))
    assert 30 <= busy.value.retry_after_s <= 600
    runner.release.set()
    wait_for(lambda: status(queue, second.id) is JobStatus.SUCCEEDED)


def test_rejected_inputs_release_the_slot(queue: JobQueue, tmp_path: Path) -> None:
    with pytest.raises(InvalidParamsError):
        queue.submit(EchoWorkflow(), EchoParams(text="x", reject=True))
    assert queue.counts() == (0, 0)
    assert not any((tmp_path / "jobs").iterdir())


def test_cancel_queued_running_and_finished(queue: JobQueue, runner: FakeRunner) -> None:
    runner.block = True
    running, _ = queue.submit(EchoWorkflow(), EchoParams(text="a"))
    runner.started.wait(2)
    waiting, _ = queue.submit(EchoWorkflow(), EchoParams(text="b"))
    assert queue.cancel(waiting.id).status is JobStatus.CANCELED
    assert queue.cancel(running.id).status is JobStatus.CANCELED
    with pytest.raises(JobConflictError):
        queue.cancel(running.id)
    with pytest.raises(JobNotFoundError):
        queue.cancel("job_missing")


def test_unknown_job(queue: JobQueue) -> None:
    with pytest.raises(JobNotFoundError):
        queue.view("job_nope")
    with pytest.raises(JobNotFoundError):
        queue.output_path("job_nope")


def test_restart_requeues_and_fails_running(tmp_path: Path, runner: FakeRunner) -> None:
    store = JobStore(tmp_path)
    queued = store.save(JobRecord(workflow="echo", params=EchoParams(text="q").model_dump()))
    lost = store.save(JobRecord(workflow="echo", params={}).started())
    orphan = store.save(JobRecord(workflow="gone", params={}))
    q = JobQueue(
        store,
        WorkflowRegistry([EchoWorkflow()]),
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
    )
    q.start()
    try:
        wait_for(lambda: status(q, queued.id) is JobStatus.SUCCEEDED)
        wait_for(lambda: status(q, orphan.id) is JobStatus.FAILED)
        assert status(q, lost.id) is JobStatus.FAILED
    finally:
        q.stop(timeout_s=5)


def test_phase_progress_mapping() -> None:
    assert _phase_progress("denoise", 0.0) == 0.05
    assert _phase_progress("denoise", 1.0) == 0.9
    assert _phase_progress("vae decode", 1.0) == 0.99
    assert _phase_progress("load", 0.5) is None


def test_idempotency_key_returns_the_same_job(queue: JobQueue, runner: FakeRunner) -> None:
    runner.block = True
    first, created = queue.submit(EchoWorkflow(), EchoParams(text="a"), "gen-1")
    runner.started.wait(2)
    again, created_again = queue.submit(EchoWorkflow(), EchoParams(text="a"), "gen-1")
    assert created and not created_again and again.id == first.id
    with pytest.raises(IdempotencyConflictError, match="different params"):
        queue.submit(EchoWorkflow(), EchoParams(text="b"), "gen-1")
    # a known key is never refused as busy, a new one is once the queue is full
    queue.submit(EchoWorkflow(), EchoParams(text="w"), "gen-2")
    assert queue.refusal("echo", "gen-1") is None
    assert queue.refusal("echo", "gen-3") is not None
    assert queue.refusal("echo", None) is not None


def test_idempotency_index_survives_restart(tmp_path: Path, runner: FakeRunner) -> None:
    store = JobStore(tmp_path)
    q1 = JobQueue(
        store,
        WorkflowRegistry([EchoWorkflow()]),
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
    )
    first, _ = q1.submit(EchoWorkflow(), EchoParams(text="a"), "k")
    store.save(first.canceled())  # finished, so the restarted queue does not run it
    q2 = JobQueue(
        store,
        WorkflowRegistry([EchoWorkflow()]),
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
    )
    q2.start()
    try:
        again, created = q2.submit(EchoWorkflow(), EchoParams(text="a"), "k")
        assert again.id == first.id and not created
        store.delete(first.id)  # pruned: the key is free again
        fresh, created = q2.submit(EchoWorkflow(), EchoParams(text="a"), "k")
        assert created and fresh.id != first.id
    finally:
        q2.stop(timeout_s=5)


def test_in_flight_key_conflicts(tmp_path: Path, runner: FakeRunner) -> None:
    q = JobQueue(
        JobStore(tmp_path),
        WorkflowRegistry([EchoWorkflow()]),
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
    )
    q._pending_keys.add(("echo", "busy-key"))
    with pytest.raises(IdempotencyConflictError, match="still being processed") as info:
        q.submit(EchoWorkflow(), EchoParams(text="a"), "busy-key")
    assert info.value.retryable and info.value.code == "idempotency_in_flight"


def test_redact_hides_home_directories() -> None:
    home = str(Path.home())
    assert redact(f"cannot open {home}/work/x.mp4") == "cannot open ~/work/x.mp4"
    assert redact("see /Users/alice/a and /home/bob/b") == "see ~/a and ~/b"
    assert redact("no paths here") == "no paths here"
