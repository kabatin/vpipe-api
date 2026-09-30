"""Built-in workflows. Registration is explicit: add new workflows to ``build_registry``."""

from __future__ import annotations

from vpipe_api.settings import Settings
from vpipe_api.workflows.base import MediaTools, Workflow, WorkflowRegistry
from vpipe_api.workflows.h3_video import H3VideoWorkflow


def build_registry(settings: Settings) -> WorkflowRegistry:
    media = MediaTools(ffmpeg=settings.ffmpeg, ffprobe=settings.ffprobe)
    workflows: list[Workflow] = [
        H3VideoWorkflow(media, settings.workflow_options(H3VideoWorkflow.id)),
    ]
    return WorkflowRegistry(workflows)


__all__ = ["WorkflowRegistry", "build_registry"]
