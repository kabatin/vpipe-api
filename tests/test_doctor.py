import subprocess
from pathlib import Path

import pytest

from tests.conftest import install_models, needs_ffmpeg
from vpipe_api import doctor
from vpipe_api.doctor import Level, check_memory, check_power, run_doctor
from vpipe_api.settings import Settings, load_settings
from vpipe_api.workflows import build_registry


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


def test_smoke_failure_keeps_log(
    settings: Settings, work_dir: Path, healthy: None, fake_mode
) -> None:
    fake_mode("stage_fail")
    install_models(work_dir)
    smoke = by_name(run_doctor(settings, build_registry(settings), smoke=True))[
        "smoke minimax-h3-turbo-video"
    ]
    assert smoke.level is Level.FAIL and "vpipe.log" in smoke.detail
