"""Job records. Immutable: every state change produces a new record via ``model_copy``."""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Crockford base32 without I, L, O, U: readable, URL-safe, sortable by creation time.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_job_id() -> str:
    millis = int(time.time() * 1000)
    stamp = "".join(_ALPHABET[(millis >> (5 * i)) & 31] for i in reversed(range(10)))
    rand = "".join(secrets.choice(_ALPHABET) for _ in range(16))
    return f"job_{stamp}{rand}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def finished(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED)


class JobError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    retryable: bool


class JobRecord(BaseModel):
    """What is persisted per job (``job.json``). Params never contain inline file data."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=new_job_id)
    workflow: str
    status: JobStatus = JobStatus.QUEUED
    params: dict[str, Any]
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: JobError | None = None
    output_file: str | None = None
    # Idempotency-Key sent with the submit, and a hash of the submitted params
    idempotency_key: str | None = None
    fingerprint: str | None = None

    def started(self) -> JobRecord:
        return self.model_copy(update={"status": JobStatus.RUNNING, "started_at": utcnow()})

    def succeeded(self, result: dict[str, Any], output_file: str) -> JobRecord:
        return self.model_copy(
            update={
                "status": JobStatus.SUCCEEDED,
                "finished_at": utcnow(),
                "result": result,
                "output_file": output_file,
            }
        )

    def failed(self, code: str, message: str, *, retryable: bool) -> JobRecord:
        return self.model_copy(
            update={
                "status": JobStatus.FAILED,
                "finished_at": utcnow(),
                "error": JobError(code=code, message=message, retryable=retryable),
            }
        )

    def canceled(self) -> JobRecord:
        return self.model_copy(update={"status": JobStatus.CANCELED, "finished_at": utcnow()})
