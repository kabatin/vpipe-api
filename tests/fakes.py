"""In-memory stand-ins for queue/API tests: a trivial workflow and a scriptable runner."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from vpipe_api.runner import ProgressCallback, RunResult
from vpipe_api.workflows.base import (
    InvalidParamsError,
    MediaTools,
    PreparedRun,
    RequiredModel,
    Workflow,
    WorkflowFailedError,
    WorkflowOutput,
)


class EchoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    fail_finalize: bool = False
    reject: bool = False


class EchoWorkflow(Workflow):
    id = "echo"
    description = "writes the text to a file"
    output_media_type = "text/plain"
    params_model = EchoParams

    def __init__(self) -> None:
        super().__init__(MediaTools())

    @property
    def required_models(self) -> tuple[RequiredModel, ...]:
        return ()

    def store_inputs(self, params: BaseModel, job_dir: Path) -> dict[str, Any]:
        p = cast(EchoParams, params)
        if p.reject:
            raise InvalidParamsError("rejected on purpose")
        job_dir.mkdir(parents=True, exist_ok=True)
        return p.model_dump()

    def estimate_seconds(self, params: Mapping[str, Any]) -> float:
        return 10.0

    def prepare(self, job_id: str, params: Mapping[str, Any], job_dir: Path) -> PreparedRun:
        return PreparedRun(spec={"text": params["text"]}, raw_output=job_dir / "raw.txt")

    def finalize(
        self, job_id: str, params: Mapping[str, Any], job_dir: Path, prepared: PreparedRun
    ) -> WorkflowOutput:
        if params["fail_finalize"]:
            raise WorkflowFailedError("postprocess_failed", "boom", retryable=False)
        out = job_dir / "output.txt"
        out.write_text(prepared.raw_output.read_text().upper())
        return WorkflowOutput(file=out, result={"length": len(params["text"])})

    def smoke_params(self) -> dict[str, Any]:
        return {"text": "hi"}


class FakeRunner:
    """Writes ``spec['text']`` to the raw output. ``block`` makes runs wait for ``release``."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.block = False
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = 0

    def run(
        self,
        spec: dict,
        run_dir: Path,
        *,
        expected_outputs: list[Path],
        timeout_s: float,
        cancel: threading.Event,
        on_progress: ProgressCallback | None = None,
    ) -> RunResult:
        self.calls += 1
        self.started.set()
        if on_progress is not None:
            on_progress("denoise", 0.5)
            on_progress("vae decode", 0.5)
            on_progress("other", 0.5)
        if self.block:
            while not self.release.is_set() and not cancel.is_set():
                self.release.wait(0.05)
        log = run_dir / "vpipe.log"
        if cancel.is_set():
            return RunResult(returncode=0, duration_s=0, log_path=log, canceled=True)
        if self.mode == "crash":
            raise RuntimeError("runner exploded")
        if self.mode == "timeout":
            return RunResult(returncode=None, duration_s=9, log_path=log, timed_out=True)
        if self.mode == "fail":
            return RunResult(
                returncode=0,
                duration_s=1,
                log_path=log,
                failures=("stage 'generate-video' process: oom",),
            )
        expected_outputs[0].write_text(spec["text"])
        return RunResult(returncode=0, duration_s=1, log_path=log)
