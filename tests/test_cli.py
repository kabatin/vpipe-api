from pathlib import Path

import pytest

from tests.conftest import install_models
from vpipe_api import cli, doctor
from vpipe_api.jobs.store import JobStore


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_vpipe: Path, work_dir: Path) -> Path:
    monkeypatch.setenv("VPIPE_API_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("VPIPE_API_VPIPE_BIN", str(fake_vpipe))
    monkeypatch.setenv("VPIPE_API_WORK_DIR", str(work_dir))
    monkeypatch.setenv("VPIPE_API_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VPIPE_API_TOKEN", "test-token-0123456789-abcdefghijklmnop")
    return work_dir


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "vpipe-api 0.1.3" in capsys.readouterr().out


def test_workflows_and_config(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["workflows"]) == 0
    out = capsys.readouterr().out
    assert "minimax-h3-turbo-video" in out and "missing" in out
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "token = ********" in out and "test-token-0123456789-abcdefghijklmnop" not in out


def test_doctor_exit_codes(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        doctor, "check_platform", lambda: doctor.Check("platform", doctor.Level.OK, "")
    )
    monkeypatch.setattr(doctor, "check_power", lambda: doctor.Check("power", doctor.Level.OK, ""))
    monkeypatch.setattr(doctor, "check_memory", lambda: doctor.Check("memory", doctor.Level.OK, ""))
    assert cli.main(["doctor"]) == 1
    install_models(env)
    assert cli.main(["doctor"]) == 0
    assert "all good" in capsys.readouterr().out


def test_setup_models_nothing_to_do(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_models(env)
    assert cli.main(["setup", "models", "minimax-h3-turbo-video"]) == 0
    assert "all models present" in capsys.readouterr().out
    assert cli.main(["setup", "models", "nope"]) == 2


def test_setup_models_asks_first(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert cli.main(["setup", "models", "minimax-h3-turbo-video"]) == 1


def test_setup_vpipe_prints_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VPIPE_API_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setattr(cli, "build_vpipe", lambda src, **_: src / "build/apps/vpipe/vpipe")
    assert cli.main(["setup", "vpipe", "--dir", str(tmp_path / "v")]) == 0
    out = capsys.readouterr().out
    assert 'vpipe_bin = "' in out and (tmp_path / "work").is_dir()


def test_settings_errors_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VPIPE_API_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setenv("VPIPE_API_HOST", "0.0.0.0")
    monkeypatch.delenv("VPIPE_API_TOKEN", raising=False)
    assert cli.main(["config"]) == 1
    assert "VPIPE_API_TOKEN" in capsys.readouterr().err


def test_serve_wires_uvicorn(env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict = {}

    def fake_run(app, **kwargs):
        seen.update(kwargs, app=app)

    monkeypatch.setattr("uvicorn.run", fake_run)
    assert cli.main(["serve", "--port", "9999"]) == 0
    assert seen["port"] == 9999 and seen["workers"] == 1 and seen["host"] == "127.0.0.1"


def test_serve_refuses_second_instance(
    env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    holder = JobStore(tmp_path / "data")
    holder.acquire_instance_lock()
    try:
        assert cli.main(["serve"]) == 1
        assert "already" in capsys.readouterr().err
    finally:
        holder.release_instance_lock()
