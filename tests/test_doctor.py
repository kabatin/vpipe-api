import subprocess
from pathlib import Path

import pytest

from tests.conftest import install_models, needs_ffmpeg
from tests.fakes import EchoWorkflow
from vpipe_api import doctor
from vpipe_api.doctor import Level, check_memory, check_models, check_power, run_doctor
from vpipe_api.settings import Settings, load_settings
from vpipe_api.workflows import build_registry
from vpipe_api.workflows.base import RequiredModel


def fake_run(outputs: dict[str, str]):
    def run(cmd: list[str], timeout: float = 20):
        for key, stdout in outputs.items():
            if key in cmd[0]:
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        return None

    return run


@pytest.fixture
def healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    # CI also runs on Linux: pretend to be the Apple Silicon Mac vpipe needs
    monkeypatch.setattr(doctor, "check_platform", lambda: doctor.Check("platform", Level.OK, ""))
    monkeypatch.setattr(
        doctor,
        "_run",
        fake_run(
            {
                "pmset": "Now drawing from 'AC Power'",
                "memory_pressure": "System-wide memory free percentage: 64%",
                "vpipe": "vpipe -- command-line entrance to libvpipe.",
            }
        ),
    )


def by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


def test_missing_models_fail(settings: Settings, healthy: None) -> None:
    checks = by_name(run_doctor(settings, build_registry(settings)))
    assert checks["vpipe"].level is Level.OK
    assert checks["work_dir"].level is Level.OK
    model = checks["model local/MiniMax-H3-FL2VA-8bit"]
    assert model.level is Level.FAIL and "setup models" in model.detail
    assert checks["power"].level is Level.OK and checks["memory"].level is Level.OK


class _NeedsA(EchoWorkflow):
    id = "needs-a"

    @property
    def required_models(self) -> tuple[RequiredModel, ...]:
        return (RequiredModel(key="org/a", path="org/a", files=("a.bin",)),)


class _NeedsB(_NeedsA):
    id = "needs-b"

    @property
    def required_models(self) -> tuple[RequiredModel, ...]:
        return (RequiredModel(key="org/b", path="org/b", files=("b.bin",)),)


def test_a_workflow_without_its_models_warns_while_another_can_run(
    settings: Settings, work_dir: Path
) -> None:
    workflows = [_NeedsA(), _NeedsB()]
    assert {c.level for c in check_models(settings, workflows)} == {Level.FAIL}  # nothing can run
    (work_dir / "models" / "org" / "a").mkdir(parents=True)
    (work_dir / "models" / "org" / "a" / "a.bin").write_bytes(b"x")
    levels = {c.name: c.level for c in check_models(settings, workflows)}
    assert levels == {"model org/a": Level.OK, "model org/b": Level.WARN}


def test_all_present(settings: Settings, work_dir: Path, healthy: None) -> None:
    install_models(work_dir)
    emitted: list[doctor.Check] = []
    checks = run_doctor(settings, build_registry(settings), emit=emitted.append)
    models = [c for c in checks if c.name.startswith("model ")]
    assert models and all(c.level is Level.OK for c in models)
    assert emitted == checks


def test_unconfigured(tmp_path: Path, healthy: None) -> None:
    s = load_settings(env={"VPIPE_API_DATA_DIR": str(tmp_path)}, config_path=tmp_path / "x")
    checks = by_name(run_doctor(s, build_registry(s)))
    assert checks["vpipe"].level is Level.FAIL and checks["work_dir"].level is Level.FAIL


def test_power_and_memory_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        "_run",
        fake_run(
            {
                "pmset": "Now drawing from 'Battery Power'",
                "memory_pressure": "System-wide memory free percentage: 5%",
            }
        ),
    )
    assert check_power().level is Level.WARN
    assert check_memory().level is Level.WARN
    monkeypatch.setattr(doctor, "_run", fake_run({}))
    assert check_power().level is Level.WARN and check_memory().level is Level.WARN


def test_missing_tool() -> None:
    assert doctor.check_tool("ffmpeg", "definitely-not-a-tool").level is Level.FAIL


@needs_ffmpeg
def test_smoke_runs_a_tiny_job(settings: Settings, work_dir: Path, healthy: None) -> None:
    install_models(work_dir)
    checks = by_name(run_doctor(settings, build_registry(settings), smoke=True))
    smoke = checks["smoke minimax-h3-turbo-video"]
    assert smoke.level is Level.OK and "832x480 56 frames" in smoke.detail
    assert not any((settings.data_dir / "smoke").iterdir())
    upscale = checks["smoke flashvsr-upscale"]
    assert upscale.level is Level.OK and "21 frames" in upscale.detail


@needs_ffmpeg
def test_smoke_skips_a_workflow_without_its_models(
    settings: Settings, work_dir: Path, healthy: None
) -> None:
    import shutil

    install_models(work_dir)
    shutil.rmtree(work_dir / "models" / "JunhaoZhuang")
    checks = by_name(run_doctor(settings, build_registry(settings), smoke=True))
    assert checks["smoke minimax-h3-turbo-video"].level is Level.OK
    skipped = checks["smoke flashvsr-upscale"]
    assert skipped.level is Level.WARN and "model missing" in skipped.detail


def test_smoke_failure_keeps_log(
    settings: Settings, work_dir: Path, healthy: None, fake_mode
) -> None:
    fake_mode("stage_fail")
    install_models(work_dir)
    smoke = by_name(run_doctor(settings, build_registry(settings), smoke=True))[
        "smoke minimax-h3-turbo-video"
    ]
    assert smoke.level is Level.FAIL and "vpipe.log" in smoke.detail


def test_smoke_reports_a_workflow_that_cannot_build_its_params(
    settings: Settings, work_dir: Path, healthy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vpipe_api.workflows.flashvsr import FlashVsrWorkflow

    install_models(work_dir)

    def broken(self: FlashVsrWorkflow) -> dict:
        raise RuntimeError("ffmpeg has no libx264")

    monkeypatch.setattr(FlashVsrWorkflow, "smoke_params", broken)
    checks = by_name(run_doctor(settings, build_registry(settings), smoke=True))
    smoke = checks["smoke flashvsr-upscale"]
    assert smoke.level is Level.FAIL and "libx264" in smoke.detail
