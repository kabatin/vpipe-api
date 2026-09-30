"""``vpipe-api doctor``: check that this machine can actually serve the registered workflows."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vpipe_api.runner import VpipeRunner
from vpipe_api.settings import Settings
from vpipe_api.workflows.base import Workflow, WorkflowFailedError, WorkflowRegistry

MIN_FREE_GB = 20
MIN_MEMORY_FREE_PERCENT = 20
SMOKE_TIMEOUT_S = 1800


class Level(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str


def _run(cmd: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_platform() -> Check:
    system, machine = platform.system(), platform.machine()
    if system == "Darwin" and machine == "arm64":
        return Check("platform", Level.OK, f"macOS {platform.mac_ver()[0]} on Apple Silicon")
    return Check(
        "platform",
        Level.FAIL,
        f"{system}/{machine}: vpipe's generative stack needs an Apple Silicon Mac",
    )


def check_vpipe(settings: Settings) -> Check:
    if settings.vpipe_bin is None:
        return Check("vpipe", Level.FAIL, "vpipe_bin is not configured")
    result = _run([str(settings.vpipe_bin), "--help"])
    if result is None or "vpipe" not in (result.stdout + result.stderr):
        return Check("vpipe", Level.FAIL, f"cannot run {settings.vpipe_bin}")
    return Check("vpipe", Level.OK, str(settings.vpipe_bin))


def check_tool(name: str, executable: str) -> Check:
    found = shutil.which(executable)
    if found is None:
        return Check(name, Level.FAIL, f"{executable} not found on PATH")
    return Check(name, Level.OK, found)


def check_work_dir(settings: Settings) -> Check:
    if settings.work_dir is None:
        return Check("work_dir", Level.FAIL, "work_dir is not configured")
    if not settings.work_dir.is_dir():
        return Check("work_dir", Level.FAIL, f"{settings.work_dir} does not exist")
    return Check("work_dir", Level.OK, str(settings.work_dir))


def check_models(settings: Settings, workflows: Iterable[Workflow]) -> list[Check]:
    if settings.work_dir is None:
        return []
    checks = []
    for workflow in workflows:
        for model in workflow.required_models:
            present = model.is_present(settings.work_dir)
            hint = (
                ""
                if present
                else (
                    f" — run `vpipe-api setup models {workflow.id}`"
                    if model.prepare_pipelines
                    else ""
                )
            )
            checks.append(
                Check(
                    f"model {model.key}",
                    Level.OK if present else Level.FAIL,
                    f"{'present' if present else 'missing'} ({workflow.id}){hint}",
                )
            )
    return checks


def check_config_file(path: Path | None) -> Check:
    if path is None or not path.is_file():
        return Check("config file", Level.OK, "none (environment only)")
    loose = path.stat().st_mode & 0o077
    if loose and "token" in path.read_text(encoding="utf-8", errors="replace"):
        return Check(
            "config file",
            Level.WARN,
            f"{path} holds the token but others can read it — chmod 600 {path}",
        )
    return Check("config file", Level.OK, str(path))


def check_disk(path: Path | None) -> Check:
    target = path if path is not None and path.exists() else Path.home()
    free_gb = shutil.disk_usage(target).free / 1e9
    level = Level.OK if free_gb >= MIN_FREE_GB else Level.WARN
    return Check("disk", level, f"{free_gb:.0f} GB free at {target}")


def check_power() -> Check:
    result = _run(["pmset", "-g", "batt"])
    if result is None:
        return Check("power", Level.WARN, "pmset unavailable")
    if "AC Power" in result.stdout:
        return Check("power", Level.OK, "on AC power")
    return Check("power", Level.WARN, "on battery: long generations drain it and run slower")


def check_memory() -> Check:
    result = _run(["memory_pressure", "-Q"])
    match = re.search(r"free percentage: (\d+)%", result.stdout if result else "")
    if match is None:
        return Check("memory", Level.WARN, "memory_pressure unavailable")
    free = int(match.group(1))
    level = Level.OK if free >= MIN_MEMORY_FREE_PERCENT else Level.WARN
    return Check("memory", level, f"{free}% free (other heavy GPU apps slow generation down)")


def run_smoke(settings: Settings, workflow: Workflow) -> Check:
    """Run the workflow's cheapest job end-to-end, bypassing the HTTP server."""
    vpipe_bin, work_dir = settings.require_runtime()
    params = workflow.params_model.model_validate(workflow.smoke_params())
    smoke_root = settings.data_dir / "smoke"
    smoke_root.mkdir(parents=True, exist_ok=True)
    job_dir = Path(tempfile.mkdtemp(prefix=f"{workflow.id}-", dir=smoke_root))
    name = f"smoke {workflow.id}"
    stored = workflow.store_inputs(params, job_dir)
    prepared = workflow.prepare("smoke", stored, job_dir)
    result = VpipeRunner(vpipe_bin, work_dir).run(
        prepared.spec,
        job_dir,
        expected_outputs=[prepared.raw_output],
        timeout_s=SMOKE_TIMEOUT_S,
        cancel=threading.Event(),
    )
    if not result.ok:
        return Check(name, Level.FAIL, f"{result.describe_failure()} — log: {result.log_path}")
    try:
        output = workflow.finalize("smoke", stored, job_dir, prepared)
    except WorkflowFailedError as exc:
        return Check(name, Level.FAIL, f"{exc} — files kept in {job_dir}")
    size = output.result.get("output", {})
    shutil.rmtree(job_dir, ignore_errors=True)
    return Check(
        name,
        Level.OK,
        f"{size.get('width')}x{size.get('height')} "
        f"{size.get('frames')} frames in {result.duration_s:.0f}s",
    )


def run_doctor(
    settings: Settings,
    registry: WorkflowRegistry,
    *,
    smoke: bool = False,
    emit: Callable[[Check], None] | None = None,
    config_path: Path | None = None,
) -> list[Check]:
    checks: list[Check] = [
        check_platform(),
        check_config_file(config_path),
        check_vpipe(settings),
        check_work_dir(settings),
        check_tool("ffmpeg", settings.ffmpeg),
        check_tool("ffprobe", settings.ffprobe),
        *check_models(settings, registry),
        check_disk(settings.work_dir),
        check_power(),
        check_memory(),
    ]
    for check in checks:
        if emit is not None:
            emit(check)
    if smoke and not any(check.level is Level.FAIL for check in checks):
        for workflow in registry:
            check = run_smoke(settings, workflow)
            checks.append(check)
            if emit is not None:
                emit(check)
    return checks
