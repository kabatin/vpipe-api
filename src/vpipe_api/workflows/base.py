"""The workflow contract: what a server-registered generation recipe must provide.

A workflow turns validated params into a vpipe pipeline spec, and the raw pipeline output
into the final deliverable. Params are validated at submit time (``params_model``); any
inline files (e.g. base64 images) are written into the job directory by ``store_inputs`` so
the persisted record only holds JSON-safe values and can be re-run after a restart.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

# vpipe's progress lines (phase, 0..1) -> the job's progress 0..1, or None to ignore one
ProgressMapper = Callable[[str, float], "float | None"]


def phase_progress(phase: str, fraction: float) -> float | None:
    """One denoise then one VAE decode, as in an H3 run."""
    if phase == "denoise":
        return round(0.05 + 0.85 * fraction, 3)
    if phase == "vae decode":
        return round(0.9 + 0.09 * fraction, 3)
    return None


class InvalidParamsError(ValueError):
    """Params passed schema validation but cannot be used (e.g. an undecodable image)."""


class WorkflowFailedError(RuntimeError):
    """Generation or post-processing failed; ``retryable`` hints whether a retry may help."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


@dataclass(frozen=True)
class RequiredModel:
    """A model a workflow needs inside ``<work_dir>/models`` (checked by ``doctor``)."""

    key: str
    path: str
    files: tuple[str, ...] = ()
    prepare_pipelines: tuple[str, ...] = ()
    # Used as published: ``setup`` fetches ``path`` from the Hugging Face Hub, nothing more
    hf_fetch: bool = False
    disk_gb_needed: int = 0

    @property
    def can_setup(self) -> bool:
        return bool(self.prepare_pipelines) or self.hf_fetch

    def is_present(self, work_dir: Path) -> bool:
        root = work_dir / "models" / self.path
        return root.is_dir() and all((root / name).is_file() for name in self.files)


@dataclass(frozen=True)
class PreparedRun:
    spec: dict[str, Any]
    raw_output: Path


@dataclass(frozen=True)
class WorkflowOutput:
    file: Path
    result: dict[str, Any]


class Workflow(ABC):
    id: ClassVar[str]
    description: ClassVar[str]
    output_media_type: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]

    def __init__(self, media: MediaTools) -> None:
        self.media = media

    @property
    @abstractmethod
    def required_models(self) -> tuple[RequiredModel, ...]: ...

    def missing_models(self, work_dir: Path) -> tuple[RequiredModel, ...]:
        return tuple(model for model in self.required_models if not model.is_present(work_dir))

    @abstractmethod
    def store_inputs(self, params: BaseModel, job_dir: Path) -> dict[str, Any]:
        """Persist inline inputs; return the JSON-safe params stored on the job record."""

    @abstractmethod
    def estimate_seconds(self, params: Mapping[str, Any]) -> float: ...

    @abstractmethod
    def prepare(self, job_id: str, params: Mapping[str, Any], job_dir: Path) -> PreparedRun: ...

    @abstractmethod
    def finalize(
        self, job_id: str, params: Mapping[str, Any], job_dir: Path, prepared: PreparedRun
    ) -> WorkflowOutput: ...

    def progress_mapper(self, params: Mapping[str, Any]) -> ProgressMapper:
        """How this job's vpipe progress lines become one 0..1 figure."""
        return phase_progress

    @abstractmethod
    def smoke_params(self) -> dict[str, Any]:
        """Cheapest valid params, used by ``vpipe-api doctor --smoke``."""


class WorkflowRegistry:
    def __init__(self, workflows: Iterable[Workflow]) -> None:
        self._by_id: dict[str, Workflow] = {}
        for workflow in workflows:
            if workflow.id in self._by_id:
                raise ValueError(f"duplicate workflow id {workflow.id!r}")
            self._by_id[workflow.id] = workflow

    def get(self, workflow_id: str) -> Workflow | None:
        return self._by_id.get(workflow_id)

    def __iter__(self) -> Iterator[Workflow]:
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)
