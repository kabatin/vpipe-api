import json
import tomllib
from pathlib import Path

import pytest

from tests.conftest import install_models
from vpipe_api import cli, doctor, setup_cli
from vpipe_api.jobs.store import JobStore
from vpipe_api.setup.command import SetupDeps
from vpipe_api.setup.machine import Machine


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
    assert "vpipe-api 0.1.4" in capsys.readouterr().out


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


def test_setup_vpipe_writes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "cfg.toml"
    monkeypatch.setenv("VPIPE_API_CONFIG", str(config))
    monkeypatch.setattr(cli, "build_vpipe", lambda src, **_: src / "build/apps/vpipe/vpipe")
    assert cli.main(["setup", "vpipe", "--dir", str(tmp_path / "v")]) == 0
    assert "config updated" in capsys.readouterr().out and (tmp_path / "work").is_dir()
    data = tomllib.loads(config.read_text())
    assert data["vpipe_bin"] == str(tmp_path / "v/build/apps/vpipe/vpipe")
    assert data["work_dir"] == str(tmp_path / "work")


def _fake_deps(tmp_path: Path) -> SetupDeps:
    def no_work(*_args, **_kwargs):
        raise AssertionError("--check must not build, prepare or load anything")

    return SetupDeps(
        machine=lambda _: Machine(False, "15.7.4", 16, 10**9, {"git": True}),
        build=no_work,
        preparer=no_work,
        home=tmp_path / "home",
        install_service=no_work,
    )


def test_setup_check_json_is_the_last_stdout_line(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "keep.toml"
    config.write_text("retention_days = 3\n")
    monkeypatch.setenv("VPIPE_API_CONFIG", str(config))
    monkeypatch.setattr(setup_cli, "default_deps", lambda: _fake_deps(tmp_path))
    assert cli.main(["setup", "--check", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    report = json.loads(out)
    assert report["action"] == "check" and report["configPath"] == str(config)
    assert [c["status"] for c in report["components"]] == ["unsupported", "unsupported"]
    assert "test-token" not in out and report["tokenSet"] is True
    assert config.read_text() == "retention_days = 3\n"
    assert cli.main(["setup", "--check", "--models", "flashvsr"]) == 0
    text = capsys.readouterr().out
    assert "flashvsr  unsupported" in text and "h3" not in text.split("\n", 1)[1]


def test_setup_bad_args_exit_2(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["setup", "--models", "h3,wan"]) == 2
    assert "valid: h3, flashvsr" in capsys.readouterr().err
    assert cli.main(["setup", "--coordinate-with", "nope"]) == 2


def test_setup_json_error_is_still_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("not [toml")
    monkeypatch.setenv("VPIPE_API_CONFIG", str(bad))
    assert cli.main(["setup", "--check", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


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


PEERS = (
    "[[coordinate_with]]\n"
    'name = "ace-step-api"\n'
    'health_url = "http://127.0.0.1:8768/v1/health"\n'
    'token = "peer-token-0123456789"\n'
)


def test_config_shows_peers_without_their_tokens(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "peers.toml"
    cfg.write_text(PEERS)
    monkeypatch.setenv("VPIPE_API_CONFIG", str(cfg))
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert (
        "[[coordinate_with]] name = ace-step-api, "
        "health_url = http://127.0.0.1:8768/v1/health, token = ********"
    ) in out
    assert "settle_seconds = 30.0" in out and "peer-token-0123456789" not in out


def test_serve_says_which_servers_it_waits_for(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "peers.toml"
    cfg.write_text(PEERS)
    monkeypatch.setenv("VPIPE_API_CONFIG", str(cfg))
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
    assert cli.main(["serve", "--port", "9999"]) == 0  # --port re-validates the settings
    assert "waits for: ace-step-api" in capsys.readouterr().out


SETUP_KEYS = {
    "server", "version", "ok", "action", "url", "lan", "tokenSet", "configPath",
    "service", "machine", "components", "error",
}  # fmt: skip


@pytest.mark.parametrize(
    "argv",
    [
        ["--label-suffix", "../x", "--port", "9000"],
        ["--label-suffix", "a" * 33, "--port", "9000"],
        ["--label-suffix", "test"],  # beside production it needs its own port
        ["--port", "nope"],
        ["--bogus"],
        ["--models", "wan"],
    ],
)
def test_setup_usage_errors_are_json_exit_2(
    env: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["setup", "--json", *argv]) == 2
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    report = json.loads(out)
    assert set(report) == SETUP_KEYS and report["ok"] is False and report["error"]


def test_setup_rejects_bad_tokens_before_doing_anything(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(setup_cli, "default_deps", lambda: pytest.fail("must not get this far"))
    monkeypatch.setenv("VPIPE_API_TOKEN", "too short")
    assert cli.main(["setup", "--lan", "--yes"]) == 2
    assert "too short" not in capsys.readouterr().err
    monkeypatch.setenv("VPIPE_API_TOKEN", "test-token-0123456789-abcdefghijklmnop")
    monkeypatch.setenv("VPIPE_API_COORD_TOKEN_SFX_API", "has a space")
    url = "sfx-api=http://127.0.0.1:8769/v1/health"
    assert cli.main(["setup", "--coordinate-with", url]) == 2
    assert "has a space" not in capsys.readouterr().err


def test_setup_label_suffix_with_port_is_accepted(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup_cli, "default_deps", lambda: _fake_deps(tmp_path))
    assert cli.main(["setup", "--check", "--label-suffix", "verify-1", "--port", "9000"]) == 0
