"""Response models (they drive ``/openapi.json``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from vpipe_api.jobs.models import JobError, JobStatus
from vpipe_api.jobs.queue import JobView


class HealthResponse(BaseModel):
    status: str
    version: str
    running: int
    waiting: int
    max_waiting: int


class WorkflowInfo(BaseModel):
    id: str
    description: str
    params_schema: dict[str, Any]
    output_media_type: str


class WorkflowsResponse(BaseModel):
    workflows: list[WorkflowInfo]


_ESTIMATE = Field(
    default=None,
    description="seconds the run is expected to take once it starts (the workflow's estimate)",
)


class JobAccepted(BaseModel):
    id: str
    workflow: str
    status: JobStatus
    created_at: datetime
    estimate_seconds: float | None = _ESTIMATE


class JobResponse(BaseModel):
    id: str
    workflow: str
    status: JobStatus
    progress: float | None
    queue_position: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict[str, Any] | None
    error: JobError | None
    estimate_seconds: float | None = _ESTIMATE
    timings: dict[str, float] = Field(
        description="wall-clock seconds measured by the server: queue_seconds (once started), "
        "backend_seconds, postprocess_seconds, total_seconds (once finished)"
    )

    @classmethod
    def from_view(cls, view: JobView) -> JobResponse:
        record = view.record
        return cls(
            id=record.id,
            workflow=record.workflow,
            status=record.status,
            progress=view.progress,
            queue_position=view.queue_position,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            result=record.result,
            error=record.error,
            estimate_seconds=record.estimate_seconds,
            timings=record.timings,
        )
