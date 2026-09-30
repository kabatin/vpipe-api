"""One GPU slot, FIFO. ``1 running + max_waiting queued``; beyond that submissions are refused
with :class:`QueueFullError` so clients can back off instead of piling up hours of work."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from vpipe_api.jobs.models import JobRecord, JobStatus
from vpipe_api.jobs.store import JobStore
from vpipe_api.runner import ProgressCallback, RunResult
from vpipe_api.workflows.base import Workflow, WorkflowFailedError, WorkflowRegistry

log = logging.getLogger("vpipe_api.queue")

TIMEOUT_MARGIN_S = 300.0
PRUNE_INTERVAL_S = 3600.0
RETRY_AFTER_MIN_S = 30
RETRY_AFTER_MAX_S = 600
CANCEL_WAIT_S = 45.0


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


@dataclass
class _Running:
    job_id: str
    started: float
    estimate_s: float
    cancel: threading.Event


_USER_DIR = re.compile(r"/(?:Users|home)/[^/\s'\"]+")


def redact(message: str) -> str:
    """Hide the server's home directory in messages that reach clients."""
    return _USER_DIR.sub("~", message.replace(str(Path.home()), "~"))


def fingerprint(workflow_id: str, params: BaseModel) -> str:
    digest = hashlib.sha256(workflow_id.encode())
    digest.update(params.model_dump_json().encode())
    return digest.hexdigest()


def _phase_progress(phase: str, fraction: float) -> float | None:
    if phase == "denoise":
        return round(0.05 + 0.85 * fraction, 3)
    if phase == "vae decode":
        return round(0.9 + 0.09 * fraction, 3)
    return None


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
    ) -> None:
        self._store = store
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

    def counts(self) -> tuple[int, int]:
        with self._cond:
            return (1 if self._running else 0), len(self._waiting)

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
            record = self._store.save(record.model_copy(update={"params": stored}))
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
        if record.status is JobStatus.SUCCEEDED:
            progress = 1.0
        return JobView(record=record, progress=progress, queue_position=position)

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
        with self._cond:
            while not self._waiting and not self._stopping:
                self._cond.wait(timeout=PRUNE_INTERVAL_S)
                if not self._waiting and not self._stopping:
                    self._store.prune(self._retention_days)
            if self._stopping:
                return None
            return self._waiting.popleft()

    def _loop(self) -> None:
        while True:
            job_id = self._next()
            if job_id is None:
                return
            record = self._store.get(job_id)
            if record is None or record.status is not JobStatus.QUEUED:
                continue
            workflow = self._registry.get(record.workflow)
            if workflow is None:
                self._store.save(
                    record.failed("unknown_workflow", record.workflow, retryable=False)
                )
                continue
            self._execute(workflow, record)

    def _execute(self, workflow: Workflow, record: JobRecord) -> None:
        estimate = workflow.estimate_seconds(record.params)
        running = _Running(record.id, time.monotonic(), estimate, threading.Event())
        with self._cond:
            self._running = running
            self._progress[record.id] = 0.0
        record = self._store.save(record.started())
        job_dir = self._store.job_dir(record.id)

        def on_progress(phase: str, fraction: float) -> None:
            value = _phase_progress(phase, fraction)
            if value is not None:
                with self._cond:
                    self._progress[record.id] = value

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
            if result.canceled:
                self._store.save(record.canceled())
            elif not result.ok:
                code = "timeout" if result.timed_out else "generation_failed"
                self._store.save(
                    record.failed(code, redact(result.describe_failure()), retryable=True)
                )
            else:
                output = workflow.finalize(record.id, record.params, job_dir, prepared)
                rel = str(output.file.relative_to(job_dir))
                self._store.save(record.succeeded(output.result, rel))
        except WorkflowFailedError as exc:
            self._store.save(record.failed(exc.code, redact(str(exc)), retryable=exc.retryable))
        except Exception:
            log.exception("job %s crashed", record.id)
            # details go to the server log only
            self._store.save(
                record.failed("internal", "internal error (see the server log)", retryable=True)
            )
        finally:
            self._store.discard_inputs(record.id, "raw.mp4")
            with self._cond:
                self._running = None
                self._progress.pop(record.id, None)
            log.info("job %s finished in %.0fs", record.id, time.monotonic() - running.started)
