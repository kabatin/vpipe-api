import base64
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
    fingerprint,
    redact,
)
from vpipe_api.jobs.store import JobStore
from vpipe_api.workflows.base import InvalidParamsError, WorkflowRegistry, phase_progress
from vpipe_api.workflows.flashvsr import FlashVsrParams
from vpipe_api.workflows.h3_video import H3VideoParams


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
    timings = view.record.timings
    assert timings.keys() == {
        "queue_seconds",
        "backend_seconds",
        "postprocess_seconds",
        "total_seconds",
    }
    assert timings["backend_seconds"] == 1  # the runner's own measurement
    assert record.estimate_seconds == EchoWorkflow().estimate_seconds({"text": "hello"})
    assert timings["total_seconds"] >= timings["queue_seconds"] + timings["postprocess_seconds"]


@pytest.mark.parametrize(
    ("mode", "code"), [("fail", "generation_failed"), ("timeout", "timeout"), ("crash", "internal")]
)
def test_failures_are_recorded(queue: JobQueue, runner: FakeRunner, mode: str, code: str) -> None:
    runner.mode = mode
    record, _ = queue.submit(EchoWorkflow(), EchoParams(text="x"))
    wait_for(lambda: status(queue, record.id) is JobStatus.FAILED)
    error = queue.view(record.id).record.error
    assert error is not None and error.code == code and error.retryable
    timings = queue.view(record.id).record.timings
    assert "total_seconds" in timings
    if mode != "crash":  # a crash inside the runner leaves no run time to report
        assert "backend_seconds" in timings


def test_workflow_failure_keeps_its_code(queue: JobQueue) -> None:
    record, _ = queue.submit(EchoWorkflow(), EchoParams(text="x", fail_finalize=True))
    wait_for(lambda: status(queue, record.id) is JobStatus.FAILED)
    error = queue.view(record.id).record.error
    assert error is not None and (error.code, error.retryable) == ("postprocess_failed", False)
    with pytest.raises(JobConflictError):
        queue.output_path(record.id)


class _OtherEcho(EchoWorkflow):
    id = "other-echo"  # e.g. an upscale next to a generation


def test_different_workflows_share_the_one_gpu_slot(tmp_path: Path, runner: FakeRunner) -> None:
    gen, upscale = EchoWorkflow(), _OtherEcho()
    q = JobQueue(
        JobStore(tmp_path),
        WorkflowRegistry([gen, upscale]),
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
    )
    q.start()
    try:
        runner.block = True
        q.submit(gen, EchoParams(text="a"))
        runner.started.wait(2)
        second, _ = q.submit(upscale, EchoParams(text="b"))
        assert q.counts() == (1, 1)  # never two runs at once, whatever the workflow
        assert q.view(second.id).record.status is JobStatus.QUEUED
        with pytest.raises(QueueFullError):
            q.submit(gen, EchoParams(text="c"))
        runner.release.set()
        wait_for(lambda: q.view(second.id).record.status is JobStatus.SUCCEEDED)
        assert runner.calls == 2
    finally:
        runner.release.set()
        q.stop(timeout_s=5)


class _BadEstimate(EchoWorkflow):
    id = "bad-estimate"

    def estimate_seconds(self, params):  # type: ignore[override]
        if params.get("text") == "boom":
            raise ValueError("a bug in the estimate")
        return 10.0


def test_a_workflow_bug_before_the_run_fails_the_job_not_the_worker(
    tmp_path: Path, runner: FakeRunner
) -> None:
    wf = _BadEstimate()
    q = JobQueue(
        JobStore(tmp_path),
        WorkflowRegistry([wf]),
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
    )
    q.start()
    try:
        record = q._store.save(JobRecord(workflow=wf.id, params={"text": "boom"}))
        q._waiting.append(record.id)
        with q._cond:
            q._cond.notify_all()
        wait_for(lambda: q.view(record.id).record.status is JobStatus.FAILED)
        after, _ = q.submit(wf, EchoParams(text="fine"))
        wait_for(lambda: q.view(after.id).record.status is JobStatus.SUCCEEDED)  # still working
    finally:
        q.stop(timeout_s=5)


def test_jobs_wait_while_vpipe_runs_outside_the_server(tmp_path: Path, runner: FakeRunner) -> None:
    outside = [1]
    q = JobQueue(
        JobStore(tmp_path),
        WorkflowRegistry([EchoWorkflow()]),
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
        outside_runs=lambda: outside[0],
        outside_poll_s=0.05,
    )
    q.start()
    try:
        record, _ = q.submit(EchoWorkflow(), EchoParams(text="held"))
        time.sleep(0.3)
        assert runner.calls == 0  # never two vpipe runs on the GPU at once
        assert q.view(record.id).record.status is JobStatus.QUEUED
        assert q.view(record.id).queue_position == 1
        assert q.outside_runs() == 1
        outside[0] = 0  # the experiment ended
        wait_for(lambda: q.view(record.id).record.status is JobStatus.SUCCEEDED)
    finally:
        q.stop(timeout_s=5)


def _held_queue(tmp_path: Path, runner: FakeRunner, outside) -> JobQueue:
    q = JobQueue(
        JobStore(tmp_path),
        WorkflowRegistry([EchoWorkflow()]),
        runner,
        max_waiting=1,
        timeout_factor=2,
        retention_days=7,
        outside_runs=outside,
        outside_poll_s=0.05,
    )
    q.start()
    return q


def test_a_held_job_can_be_canceled_and_the_queue_stopped(
    tmp_path: Path, runner: FakeRunner
) -> None:
    q = _held_queue(tmp_path, runner, lambda: 1)
    record, _ = q.submit(EchoWorkflow(), EchoParams(text="held"))
    q.cancel(record.id)
    assert q.view(record.id).record.status is JobStatus.CANCELED
    started = time.monotonic()
    q.stop(timeout_s=5)
    assert time.monotonic() - started < 2 and runner.calls == 0


def test_a_failing_outside_check_does_not_stop_the_worker(
    tmp_path: Path, runner: FakeRunner
) -> None:
    calls = []

    def flaky() -> int | None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("ps exploded")
        return None  # unknown: does not hold jobs

    q = _held_queue(tmp_path, runner, flaky)
    try:
        record, _ = q.submit(EchoWorkflow(), EchoParams(text="x"))
        wait_for(lambda: q.view(record.id).record.status is JobStatus.SUCCEEDED)
    finally:
        q.stop(timeout_s=5)


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
    assert phase_progress("denoise", 0.0) == 0.05
    assert phase_progress("denoise", 1.0) == 0.9
    assert phase_progress("vae decode", 1.0) == 0.99
    assert phase_progress("load", 0.5) is None


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


def test_keys_made_by_0_1_2_still_match() -> None:
    # Stored jobs keep the digest they were made with; a params field added since must not
    # change it while the field holds its default. The digests were computed by 0.1.2.
    body = {"prompt": "a lake", "output": {"width": 1920, "height": 1080}, "seed": 7}
    h3 = H3VideoParams.model_validate(body | {"quality": "final"})
    old_h3 = "85b1176422e770291bfa3eed95d191461037608f81e57ca0e8a19a3f08e9446f"
    assert fingerprint("minimax-h3-turbo-video", h3) == old_h3
    native = h3.model_copy(update={"native": True})
    assert fingerprint("minimax-h3-turbo-video", native) != old_h3
    video = {"data": base64.b64encode(b"take").decode(), "media_type": "video/mp4"}
    upscale = FlashVsrParams.model_validate({"source_video": video})
    old_upscale = "3effa67d1a7390de5efcee70b7ddf887a4fb8085f269ffc4e2830e51c567f89d"
    assert fingerprint("flashvsr-upscale", upscale) == old_upscale


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
