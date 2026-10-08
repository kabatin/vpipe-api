"""FastAPI application: routes under ``/v1``. One typed POST route per registered workflow."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Header
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from vpipe_api import __version__
from vpipe_api.api.errors import (
    ErrorEnvelope,
    busy_response,
    error_response,
    install_error_handlers,
)
from vpipe_api.api.schemas import (
    HealthResponse,
    JobAccepted,
    JobResponse,
    WorkflowInfo,
    WorkflowsResponse,
)
from vpipe_api.api.security import (
    IDEMPOTENCY_HEADER,
    IDEMPOTENCY_KEY_PATTERN,
    BearerAuthMiddleware,
    BodySizeLimitMiddleware,
    BusyGateMiddleware,
    LoopbackOnlyMiddleware,
    ModelGateMiddleware,
)
from vpipe_api.jobs.queue import (
    IdempotencyConflictError,
    JobConflictError,
    JobNotFoundError,
    JobQueue,
    QueueFullError,
)
from vpipe_api.workflows.base import InvalidParamsError, Workflow, WorkflowRegistry

_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorEnvelope} for code in (401, 404, 409, 413, 422, 429)
}


def _not_found(what: str) -> JSONResponse:
    return error_response(404, "not_found", f"{what} not found")


def _submit_endpoint(queue: JobQueue, workflow: Workflow) -> Callable[..., Any]:
    def submit(params: BaseModel, idempotency_key: str | None = None) -> Any:
        try:
            record, created = queue.submit(workflow, params, idempotency_key)
        except QueueFullError as exc:
            return busy_response(exc.retry_after_s)
        except IdempotencyConflictError as exc:
            return error_response(409, exc.code, str(exc), retryable=exc.retryable)
        except InvalidParamsError as exc:
            return error_response(422, "invalid_params", str(exc))
        return JSONResponse(
            status_code=202 if created else 200,
            content=JobAccepted(
                id=record.id,
                workflow=record.workflow,
                status=record.status,
                created_at=record.created_at,
                estimate_seconds=record.estimate_seconds,
            ).model_dump(mode="json"),
        )

    # Give FastAPI the workflow's own params model so validation and OpenAPI are exact.
    submit.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                "params", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=workflow.params_model
            ),
            inspect.Parameter(
                "idempotency_key",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=str | None,
                default=Header(
                    default=None,
                    alias=IDEMPOTENCY_HEADER,
                    pattern=IDEMPOTENCY_KEY_PATTERN,
                    description="Resubmitting the same key with the same params returns the "
                    "same job (200) instead of creating another; different params → 409.",
                ),
            ),
        ]
    )
    return submit


def build_router(queue: JobQueue, registry: WorkflowRegistry) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        running, waiting = queue.counts()
        return HealthResponse(
            status="ok",
            version=__version__,
            running=running,
            waiting=waiting,
            max_waiting=queue.max_waiting,
            outside_runs=queue.outside_runs(),
        )

    @router.get("/workflows", response_model=WorkflowsResponse)
    def workflows() -> WorkflowsResponse:
        return WorkflowsResponse(
            workflows=[
                WorkflowInfo(
                    id=w.id,
                    description=w.description,
                    params_schema=w.params_model.model_json_schema(),
                    output_media_type=w.output_media_type,
                )
                for w in registry
            ]
        )

    for workflow in registry:
        router.add_api_route(
            f"/workflows/{workflow.id}/jobs",
            _submit_endpoint(queue, workflow),
            methods=["POST"],
            status_code=202,
            response_model=JobAccepted,
            responses=_ERRORS,
            summary=f"Queue a {workflow.id} job",
            operation_id=f"submit_{workflow.id.replace('-', '_')}",
            tags=["workflows"],
        )

    @router.post("/workflows/{workflow_id}/jobs", include_in_schema=False)
    def unknown_workflow(workflow_id: str) -> JSONResponse:
        return _not_found(f"workflow {workflow_id!r}")

    @router.get("/jobs/{job_id}", response_model=JobResponse, responses=_ERRORS)
    def get_job(job_id: str) -> Any:
        try:
            return JobResponse.from_view(queue.view(job_id))
        except JobNotFoundError:
            return _not_found("job")

    @router.get("/jobs/{job_id}/output", response_class=FileResponse, responses=_ERRORS)
    def get_output(job_id: str) -> Any:
        try:
            path = queue.output_path(job_id)
            record = queue.view(job_id).record
        except JobNotFoundError:
            return _not_found("job")
        except JobConflictError as exc:
            return error_response(409, "conflict", str(exc))
        workflow = registry.get(record.workflow)
        media_type = workflow.output_media_type if workflow else "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=f"{job_id}{path.suffix}")

    @router.delete("/jobs/{job_id}", response_model=JobResponse, responses=_ERRORS)
    def cancel_job(job_id: str) -> Any:
        try:
            queue.cancel(job_id)
            return JobResponse.from_view(queue.view(job_id))
        except JobNotFoundError:
            return _not_found("job")
        except JobConflictError as exc:
            return error_response(409, "conflict", str(exc))

    return router


def create_app(
    queue: JobQueue,
    registry: WorkflowRegistry,
    *,
    token: str | None,
    max_body_bytes: int,
    work_dir: Path | None = None,
    manage_queue: bool = True,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if manage_queue:
            queue.start()
        try:
            yield
        finally:
            if manage_queue:
                queue.stop()

    app = FastAPI(
        title="vpipe-api",
        version=__version__,
        summary="Run vpipe generative pipelines as HTTP jobs",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(build_router(queue, registry))
    # Starlette runs the last-added middleware first: auth (or loopback-only without a
    # token), size check, model gate (when work_dir is known), busy gate, route.
    app.add_middleware(BusyGateMiddleware, queue=queue)
    if work_dir is not None:
        app.add_middleware(ModelGateMiddleware, registry=registry, work_dir=work_dir)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body_bytes)
    if token is not None:
        app.add_middleware(BearerAuthMiddleware, token=token)
    else:
        app.add_middleware(LoopbackOnlyMiddleware)
    return app
