"""One GPU slot, FIFO. ``1 running + max_waiting queued``; beyond that submissions are refused
with :class:`QueueFullError` so clients can back off instead of piling up hours of work.

The job at the head of the queue starts only when no vpipe run outside the server is going on
(:mod:`vpipe_api.outside`) and the other GPU servers on the Mac are idle
(:mod:`vpipe_api.jobs.coordination`); while it waits for a server it stays ``queued`` with
``waiting_for``."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from vpipe_api.jobs.coordination import Coordinator
from vpipe_api.jobs.models import JobRecord, JobStatus
from vpipe_api.jobs.store import JobStore
from vpipe_api.runner import ProgressCallback, RunResult
from vpipe_api.workflows.base import PreparedRun, Workflow, WorkflowFailedError, WorkflowRegistry

log = logging.getLogger("vpipe_api.queue")

TIMEOUT_MARGIN_S = 300.0
PRUNE_INTERVAL_S = 3600.0
RETRY_AFTER_MIN_S = 30
RETRY_AFTER_MAX_S = 600
CANCEL_WAIT_S = 45.0
OUTSIDE_POLL_S = 5.0
WORKER_BACKOFF_S = 1.0


class Runner(Protocol):
    def run(
        self,
        spec: dict,
        run_dir: Path,
        *,
        expected_outputs: list[Path],
        timeout_s: float,
        cancel: threading.Event,
        on_progress: ProgressCallback | None = None,
    ) -> RunResult: ...


class QueueFullError(Exception):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__("the GPU slot and the waiting queue are full")
        self.retry_after_s = retry_after_s


class IdempotencyConflictError(Exception):
    """The Idempotency-Key was already used with different params (a client bug)."""

    code = "idempotency_conflict"
    retryable = False


class IdempotencyInFlightError(IdempotencyConflictError):
    """Another request with this key is being accepted right now: retry shortly."""

    code = "idempotency_in_flight"
    retryable = True


class JobNotFoundError(KeyError):
    pass


class JobConflictError(Exception):
    pass


@dataclass(frozen=True)
class JobView:
    record: JobRecord
    progress: float | None
    queue_position: int | None
    waiting_for: str | None = None


@dataclass
class _Running:
    job_id: str
    started: float
    estimate_s: float
    cancel: threading.Event


_USER_DIR = re.compile(r"/(?:Users|home)/[^/\s'\"]+")


def _since(began: float) -> float:
    return round(time.monotonic() - began, 3)


def redact(message: str) -> str:
    """Hide the server's home directory in messages that reach clients."""
    return _USER_DIR.sub("~", message.replace(str(Path.home()), "~"))


def fingerprint(workflow_id: str, params: BaseModel) -> str:
    """The params' digest for Idempotency-Key. Stored jobs keep the digest they were made
    with, so a field added to a params model later is named in its ``LATE_FIELDS`` and left
    out while it holds its default: the same request still matches keys made before."""
    fields = type(params).model_fields
    late = {
        name
        for name in getattr(type(params), "LATE_FIELDS", ())
        if getattr(params, name) == fields[name].default
    }
    digest = hashlib.sha256(workflow_id.encode())
    digest.update(params.model_dump_json(exclude=late).encode())
    return digest.hexdigest()


class JobQueue:
    def __init__(
        self,
        store: JobStore,
        registry: WorkflowRegistry,
        runner: Runner,
        *,
        max_waiting: int,
        timeout_factor: float,
        retention_days: int,
        outside_runs: Callable[[], int | None] = lambda: 0,
        outside_poll_s: float = OUTSIDE_POLL_S,
        coordinator: Coordinator | None = None,
    ) -> None:
        self._store = store
        # vpipe runs this server did not start (see vpipe_api.outside): jobs wait for them
        self._outside_runs = outside_runs
        self._outside_poll_s = outside_poll_s
        # the other GPU servers on the Mac (see vpipe_api.jobs.coordination)
        self._coordinator = coordinator if coordinator is not None and coordinator.peers else None
        self._registry = registry
        self._runner = runner
        self._max_waiting = max_waiting
        self._timeout_factor = timeout_factor
        self._retention_days = retention_days
        self._cond = threading.Condition()
        self._waiting: deque[str] = deque()
        self._reserved = 0
        self._running: _Running | None = None
        self._progress: dict[str, float] = {}
        # head-of-queue job id -> the GPU server it is waiting for
        self._waiting_for: dict[str, str] = {}
        # (workflow id, Idempotency-Key) -> job id, and keys whose submit is in flight
        self._keys: dict[tuple[str, str], str] = {}
        self._pending_keys: set[tuple[str, str]] = set()
        self._stopping = False
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        requeue, failed = self._store.recover()
        for record in failed:
            log.warning("job %s was running when the server stopped; marked failed", record.id)
        with self._cond:
            self._waiting.extend(record.id for record in requeue)
            self._keys = {
                (record.workflow, record.idempotency_key): record.id
                for record in self._store.all()
                if record.idempotency_key is not None
            }
        self._store.prune(self._retention_days)
        self._thread = threading.Thread(target=self._loop, name="vpipe-api-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 60.0) -> None:
        with self._cond:
            self._stopping = True
            if self._running is not None:
                self._running.cancel.set()
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    # -- API-facing ----------------------------------------------------------------
    @property
    def max_waiting(self) -> int:
        return self._max_waiting

    def outside_runs(self) -> int | None:
        """vpipe runs going on outside this server (None: unknown); the next job waits while
        there are any. A failing check counts as unknown and never holds a job."""
        try:
            return self._outside_runs()
        except Exception:
            log.exception("checking for vpipe runs outside the server failed")
            return None

    def counts(self) -> tuple[int, int, int]:
        """``(running, waiting, waiting_for_peer)``. While the head job waits for another GPU
        server, the whole queue is reported as ``waiting_for_peer``, not ``waiting``: servers
        that wait for this one (ace-step-api, sfx-api, klein-api) then see it idle and go
        first. Counting those jobs as ``waiting`` would deadlock two servers that each wait for
        the other. Capacity (``429``) still counts every queued job."""
        with self._cond:
            running = 1 if self._running else 0
            queued = len(self._waiting)
            head = self._waiting[0] if self._waiting else None
            if head is not None and head in self._waiting_for:
                return running, 0, queued
            return running, queued, 0

    def refusal(self, workflow_id: str, idempotency_key: str | None) -> int | None:
        """``Retry-After`` seconds if a new submit would be refused now, else ``None``.

        Lets the HTTP layer answer 429 before reading a (possibly 60 MB) body. A key that
        already has a job is never refused: resubmitting it just returns that job.
        """
        with self._cond:
            if idempotency_key is not None and (workflow_id, idempotency_key) in self._keys:
                return None
            if self._occupied_locked() >= 1 + self._max_waiting:
                return self._retry_after_locked()
            return None

    def submit(
        self, workflow: Workflow, params: BaseModel, idempotency_key: str | None = None
    ) -> tuple[JobRecord, bool]:
        """Queue a job. Returns ``(record, created)``; ``created`` is False when an
        ``idempotency_key`` resubmission returns the job it already made."""
        key = None if idempotency_key is None else (workflow.id, idempotency_key)
        params_hash = None if key is None else fingerprint(workflow.id, params)
        with self._cond:
            if key is not None:
                existing = self._existing_locked(key, params_hash)
                if existing is not None:
                    return existing, False
                self._pending_keys.add(key)
            if self._occupied_locked() >= 1 + self._max_waiting:
                if key is not None:
                    self._pending_keys.discard(key)
                raise QueueFullError(self._retry_after_locked())
            self._reserved += 1
        record = JobRecord(
            workflow=workflow.id,
            params={},
            idempotency_key=idempotency_key,
            fingerprint=params_hash,
        )
        job_dir = self._store.job_dir(record.id)
        try:
            stored = workflow.store_inputs(params, job_dir)
            estimate = round(workflow.estimate_seconds(stored), 1)
            record = self._store.save(
                record.model_copy(update={"params": stored, "estimate_seconds": estimate})
            )
        except BaseException:
            shutil.rmtree(job_dir, ignore_errors=True)
            with self._cond:
                self._reserved -= 1
                if key is not None:
                    self._pending_keys.discard(key)
            raise
        with self._cond:
            self._reserved -= 1
            if key is not None:
                self._pending_keys.discard(key)
                self._keys[key] = record.id
            self._waiting.append(record.id)
            self._cond.notify_all()
        log.info("queued %s (%s)", record.id, workflow.id)
        return record, True

    def _occupied_locked(self) -> int:
        return (1 if self._running else 0) + len(self._waiting) + self._reserved

    def _existing_locked(self, key: tuple[str, str], params_hash: str | None) -> JobRecord | None:
        if key in self._pending_keys:
            raise IdempotencyInFlightError(
                "a request with this Idempotency-Key is still being processed; retry shortly"
            )
        job_id = self._keys.get(key)
        if job_id is None:
            return None
        existing = self._store.get(job_id)
        if existing is None:  # pruned after the retention window: the key is free again
            del self._keys[key]
            return None
        if existing.fingerprint != params_hash:
            raise IdempotencyConflictError(
                "this Idempotency-Key was already used with different params"
            )
        return existing

    def view(self, job_id: str) -> JobView:
        record = self._store.get(job_id)
        if record is None:
            raise JobNotFoundError(job_id)
        with self._cond:
            position = list(self._waiting).index(job_id) + 1 if job_id in self._waiting else None
            progress = self._progress.get(job_id)
            waiting_for = self._waiting_for.get(job_id)
        if record.status is JobStatus.SUCCEEDED:
            progress = 1.0
        if record.status is not JobStatus.QUEUED:
            waiting_for = None
        return JobView(
            record=record, progress=progress, queue_position=position, waiting_for=waiting_for
        )

    def output_path(self, job_id: str) -> Path:
        record = self._store.get(job_id)
        if record is None:
            raise JobNotFoundError(job_id)
        if record.status is not JobStatus.SUCCEEDED or record.output_file is None:
            raise JobConflictError(f"job is {record.status.value}, not succeeded")
        path = self._store.job_dir(job_id) / record.output_file
        if not path.is_file():
            raise JobConflictError("the output file is gone (retention expired?)")
        return path

    def cancel(self, job_id: str) -> JobRecord:
        with self._cond:
            record = self._store.get(job_id)
            if record is None:
                raise JobNotFoundError(job_id)
            if record.status.finished:
                raise JobConflictError(f"job already {record.status.value}")
            running = self._running
            if running is None or running.job_id != job_id:
                # Not started yet (possibly not even in the deque): the worker skips any
                # record that is no longer QUEUED, so saving CANCELED is enough.
                if job_id in self._waiting:
                    self._waiting.remove(job_id)
                    self._cond.notify_all()  # a coordination wait for it ends now
                canceled = self._store.save(record.canceled())
                self._store.discard_inputs(job_id)
                return canceled
            running.cancel.set()
        deadline = time.monotonic() + CANCEL_WAIT_S
        while time.monotonic() < deadline:
            current = self._store.get(job_id)
            if current is not None and current.status.finished:
                return current
            time.sleep(0.2)
        current = self._store.get(job_id)
        if current is None:
            raise JobNotFoundError(job_id)
        return current

    # -- worker --------------------------------------------------------------------
    def _retry_after_locked(self) -> int:
        if self._running is None:
            return RETRY_AFTER_MIN_S
        left = self._running.estimate_s - (time.monotonic() - self._running.started)
        return int(min(max(left, RETRY_AFTER_MIN_S), RETRY_AFTER_MAX_S))

    def _next(self) -> str | None:
        """Wait until the head job may start, then take it off the queue. It is held while a
        vpipe run outside the server goes on, then while another GPU server is busy; a run
        outside that began during the second wait sends it back to the first."""
        held = False
        while True:
            with self._cond:
                while not self._waiting and not self._stopping:
                    self._cond.wait(timeout=PRUNE_INTERVAL_S)
                    if not self._waiting and not self._stopping:
                        self._store.prune(self._retention_days)
                if self._stopping:
                    return None
                head = self._waiting[0]
            outside = self.outside_runs()  # runs ps: not under the lock
            if outside:
                if not held:
                    log.info("holding the next job: %d vpipe run(s) outside the server", outside)
                    held = True
                with self._cond:
                    self._cond.wait_for(lambda: self._stopping, timeout=self._outside_poll_s)
                continue
            if held:
                log.info("no vpipe runs outside the server any more")
                held = False
            if not self._coordinate(head):
                continue  # canceled, no longer first, or stopping: look again
            if self._coordinator is not None and self.outside_runs():
                continue  # one began while it waited for the other servers: hold it again
            with self._cond:
                if self._stopping:
                    return None
                if self._waiting and self._waiting[0] == head:  # a cancel may have taken it
                    return self._waiting.popleft()

    def _coordinate(self, job_id: str) -> bool:
        """True once the other GPU servers let ``job_id`` start; False if it should not be
        started now (it was canceled or is no longer first, or the server is stopping)."""
        if self._coordinator is None:
            return True

        def on_waiting(peer: str) -> None:
            with self._cond:
                if job_id not in self._waiting_for:
                    log.info("job %s waits for %s to be idle", job_id, peer)
                self._waiting_for[job_id] = peer

        try:
            return self._coordinator.wait_until_idle(
                job_id, sleep=lambda seconds: self._pause(job_id, seconds), on_waiting=on_waiting
            )
        finally:
            with self._cond:
                self._waiting_for.pop(job_id, None)

    def _pause(self, job_id: str, seconds: float) -> bool:
        """Sleep while ``job_id`` stays first in line; False as soon as it is not (or the
        server stops)."""
        deadline = time.monotonic() + seconds
        with self._cond:
            while True:
                if self._stopping or not self._waiting or self._waiting[0] != job_id:
                    return False
                left = deadline - time.monotonic()
                if left <= 0:
                    return True
                self._cond.wait(timeout=left)

    def _loop(self) -> None:
        """The worker. Nothing may end it but stop(): a dead worker would leave every later
        job queued forever while the health check still looked fine."""
        while True:
            try:
                job_id = self._next()
            except Exception:
                log.exception("worker: could not take the next job; trying again")
                with self._cond:
                    self._cond.wait_for(lambda: self._stopping, timeout=WORKER_BACKOFF_S)
                continue
            if job_id is None:
                return
            try:
                self._run_one(job_id)
            except Exception:  # e.g. the disk filled up: fail the job, keep the worker
                log.exception("worker: job %s could not be concluded", job_id)
                self._fail_quietly(job_id)
            finally:
                with self._cond:
                    self._running = None
                    self._progress.pop(job_id, None)

    def _run_one(self, job_id: str) -> None:
        record = self._store.get(job_id)
        if record is None or record.status is not JobStatus.QUEUED:
            return
        workflow = self._registry.get(record.workflow)
        if workflow is None:
            self._store.save(record.failed("unknown_workflow", record.workflow, retryable=False))
            return
        self._execute(workflow, record)

    def _fail_quietly(self, job_id: str) -> None:
        """Best effort after the worker hit an error outside a job's own handling: record the
        job as failed so clients stop waiting for it."""
        try:
            record = self._store.get(job_id)
            if record is not None and not record.status.finished:
                message = "internal error (see the server log)"
                self._store.save(record.failed("internal", message, retryable=True))
            self._store.discard_inputs(job_id)
        except Exception:
            log.exception("worker: job %s could not be recorded as failed either", job_id)

    def _execute(self, workflow: Workflow, record: JobRecord) -> None:
        try:
            estimate = workflow.estimate_seconds(record.params)
            mapper = workflow.progress_mapper(record.params)
        except Exception:  # a workflow bug fails this job; the worker carries on
            log.exception("job %s: workflow failed before the run", record.id)
            message = "internal error (see the server log)"
            self._store.save(record.failed("internal", message, retryable=False))
            self._store.discard_inputs(record.id)
            return
        running = _Running(record.id, time.monotonic(), estimate, threading.Event())
        with self._cond:
            self._running = running
            self._progress[record.id] = 0.0
        record = self._store.save(record.started())
        job_dir = self._store.job_dir(record.id)

        def on_progress(phase: str, fraction: float) -> None:
            value = mapper(phase, fraction)
            if value is not None:
                with self._cond:
                    self._progress[record.id] = value

        prepared: PreparedRun | None = None
        timings: dict[str, float] = {}
        try:
            prepared = workflow.prepare(record.id, record.params, job_dir)
            result = self._runner.run(
                prepared.spec,
                job_dir,
                expected_outputs=[prepared.raw_output],
                timeout_s=estimate * self._timeout_factor + TIMEOUT_MARGIN_S,
                cancel=running.cancel,
                on_progress=on_progress,
            )
            timings = {"backend_seconds": round(result.duration_s, 3)}
            if result.canceled:
                self._store.save(record.canceled(timings))
            elif not result.ok:
                code = "timeout" if result.timed_out else "generation_failed"
                message = redact(result.describe_failure())
                self._store.save(record.failed(code, message, retryable=True, timings=timings))
            else:
                began = time.monotonic()
                output = workflow.finalize(record.id, record.params, job_dir, prepared)
                timings = {**timings, "postprocess_seconds": _since(began)}
                rel = str(output.file.relative_to(job_dir))
                self._store.save(record.succeeded(output.result, rel, timings))
        except WorkflowFailedError as exc:
            message = redact(str(exc))
            self._store.save(
                record.failed(exc.code, message, retryable=exc.retryable, timings=timings)
            )
        except Exception:
            log.exception("job %s crashed", record.id)
            # details go to the server log only
            message = "internal error (see the server log)"
            self._store.save(record.failed("internal", message, retryable=True, timings=timings))
        finally:
            raw = [prepared.raw_output.name] if prepared is not None else []
            self._store.discard_inputs(record.id, *raw)
            with self._cond:
                self._running = None
                self._progress.pop(record.id, None)
            log.info("job %s finished in %.0fs", record.id, time.monotonic() - running.started)
