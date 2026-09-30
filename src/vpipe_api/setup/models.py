"""``vpipe-api setup models <workflow>``: download and prepare the models a workflow needs.

Runs vpipe's own ``docs/pipelines/prepare-*.vpipeline`` files. The download step runs on its
own under a watchdog: vpipe's transfer only gives up below 1 KB/s, so a connection left
bound to an old IP (network switch) or a ~2 KB/s trickle would otherwise stall for hours.
When the model directory stops growing, the fetch is stopped (Ctrl-C, keeping ``.part``
files) and started again, resuming where it left off.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vpipe_api.runner import RunResult, VpipeRunner
from vpipe_api.settings import Settings
from vpipe_api.setup.vpipe_build import SetupError
from vpipe_api.workflows.base import RequiredModel, Workflow

Emit = Callable[[str], None]
STALL_S = 600.0
POLL_S = 30.0
MAX_RESTARTS = 30
PREPARE_TIMEOUT_S = 12 * 3600.0


def dir_size(root: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except FileNotFoundError:
                continue  # a .part renamed while we were walking
    return total


def fetch_only(spec: dict[str, Any]) -> dict[str, Any]:
    stages = [
        {key: value for key, value in stage.items() if key != "iports"}
        for stage in spec.get("stages", [])
        if stage.get("type") == "model-fetch"
    ]
    return {"id": f"{spec.get('id', 'prepare')}-fetch", "stages": stages}


class ModelPreparer:
    def __init__(
        self,
        settings: Settings,
        *,
        runner: VpipeRunner | None = None,
        emit: Emit = lambda _: None,
        stall_s: float = STALL_S,
        poll_s: float = POLL_S,
        max_restarts: int = MAX_RESTARTS,
    ) -> None:
        vpipe_bin, work_dir = settings.require_runtime()
        src = settings.resolved_vpipe_src_dir
        if src is None or not (src / "docs" / "pipelines").is_dir():
            raise SetupError("cannot find vpipe's docs/pipelines — set vpipe_src_dir")
        self._pipelines = src / "docs" / "pipelines"
        self._work_dir = work_dir
        self._models_dir = work_dir / "models"
        self._runner = runner or VpipeRunner(vpipe_bin, work_dir)
        self._runs_dir = settings.data_dir / "setup"
        self._emit = emit
        self._stall_s = stall_s
        self._poll_s = poll_s
        self._max_restarts = max_restarts

    def prepare(self, workflow: Workflow) -> list[str]:
        prepared: list[str] = []
        for model in workflow.required_models:
            if model.is_present(self._work_dir):
                self._emit(f"{model.key}: already present")
                continue
            self._prepare_model(model)
            prepared.append(model.key)
        return prepared

    def _prepare_model(self, model: RequiredModel) -> None:
        if not model.prepare_pipelines:
            raise SetupError(f"{model.key} has no prepare pipeline; prepare it by hand")
        self._models_dir.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(self._models_dir).free / 1e9
        if free_gb < model.disk_gb_needed:
            raise SetupError(
                f"{model.key} needs ~{model.disk_gb_needed} GB free while preparing; "
                f"only {free_gb:.0f} GB free at {self._models_dir}"
            )
        for name in model.prepare_pipelines:
            path = self._pipelines / f"{name}.vpipeline"
            if not path.is_file():
                raise SetupError(f"{path} not found — is vpipe_src_dir a vpipe checkout?")
            spec = json.loads(path.read_text(encoding="utf-8"))
            self._emit(f"{name}: downloading")
            self._fetch_with_watchdog(name, fetch_only(spec))
            self._emit(f"{name}: preparing (quantize etc.)")
            result = self._runner.run(
                spec,
                self._runs_dir / name,
                expected_outputs=[],
                timeout_s=PREPARE_TIMEOUT_S,
                cancel=threading.Event(),
            )
            if not result.ok:
                raise SetupError(f"{name} failed: {result.describe_failure()} ({result.log_path})")
        if not model.is_present(self._work_dir):
            raise SetupError(
                f"{model.key} still missing after {', '.join(model.prepare_pipelines)}"
            )

    def _fetch_with_watchdog(self, name: str, spec: dict[str, Any]) -> None:
        for attempt in range(self._max_restarts + 1):
            result, stalled = self._fetch_once(name, spec)
            partial = next(self._models_dir.rglob("*.part"), None)
            if not stalled and result is not None and result.ok and partial is None:
                return
            reason = (
                "stalled"
                if stalled
                else (
                    result.describe_failure()
                    if result is not None and not result.ok
                    else f"incomplete ({partial})"
                )
            )
            self._emit(f"{name}: {reason}; resuming ({attempt + 1}/{self._max_restarts})")
        raise SetupError(f"{name}: download did not finish after {self._max_restarts} restarts")

    def _fetch_once(self, name: str, spec: dict[str, Any]) -> tuple[RunResult | None, bool]:
        cancel = threading.Event()
        box: dict[str, RunResult] = {}

        def work() -> None:
            box["result"] = self._runner.run(
                spec,
                self._runs_dir / f"{name}-fetch",
                expected_outputs=[],
                timeout_s=PREPARE_TIMEOUT_S,
                cancel=cancel,
            )

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        last, idle, stalled = dir_size(self._models_dir), 0.0, False
        while thread.is_alive():
            thread.join(timeout=self._poll_s)
            if not thread.is_alive():
                break
            size = dir_size(self._models_dir)
            idle = idle + self._poll_s if size == last else 0.0
            last = size
            if idle >= self._stall_s:
                stalled = True
                cancel.set()
                thread.join()
        return box.get("result"), stalled
