import json
import tomllib
from pathlib import Path

import pytest

from tests.conftest import install_models
from vpipe_api.settings import Settings, load_settings
from vpipe_api.setup.command import (
    SetupDeps,
    SetupOptions,
    parse_components,
    parse_peer,
    run_setup,
)
from vpipe_api.setup.config_write import Peer
from vpipe_api.setup.machine import Machine
from vpipe_api.setup.service import ServiceState
from vpipe_api.setup.vpipe_build import SetupError, binary_path

TOKEN = "env-token-0123456789-abcdefghijklmnop"
GOOD_MAC = Machine(
    arm64=True,
    macos="26.0",
    ram_gb=32,
    free_bytes=10**15,
    tools={"git": True, "cmake": True, "uv": True, "xcode": True},
)


class Recorder:
    """Fake build / preparer / service that log what they were asked to do."""

    def __init__(self, fail_build: bool = False, fail_prepare: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_build = fail_build
        self.fail_prepare = fail_prepare

    def build(self, src: Path, emit) -> Path:
        self.calls.append(f"build {src.name}")
        if self.fail_build:
            raise SetupError("cmake build failed")
        binary_path(src).parent.mkdir(parents=True)
        binary_path(src).write_text("")
        (src / "docs" / "pipelines").mkdir(parents=True)
        return binary_path(src)

    def preparer(self, settings: Settings, emit):
        recorder = self

        class _Prep:
            def prepare(self, workflow):
                recorder.calls.append(f"prepare {workflow.id}")
                if recorder.fail_prepare:
                    raise SetupError("download did not finish")
                assert settings.work_dir is not None
                install_models(settings.work_dir)
                return [m.key for m in workflow.required_models]

        return _Prep()

    def install_service(self, **kwargs) -> ServiceState:
        self.calls.append(f"service port={kwargs['port']} token={kwargs['token'] is not None}")
        return ServiceState(True, "com.github.kabatin.vpipe-api", True)

    def service_state(self, suffix, port, token, *, home) -> ServiceState:
        return ServiceState(False, "com.github.kabatin.vpipe-api", None)


def deps(rec: Recorder, tmp_path: Path, **kw) -> SetupDeps:
    return SetupDeps(
        machine=kw.pop("machine", lambda _: GOOD_MAC),
        build=rec.build,
        preparer=rec.preparer,
        home=tmp_path / "home",
        install_service=rec.install_service,
        service_state=rec.service_state,
        **kw,
    )


@pytest.fixture
def blank(tmp_path: Path) -> Settings:
    return load_settings(env={}, config_path=tmp_path / "none.toml")


def opts(tmp_path: Path, **kw) -> SetupOptions:
    return SetupOptions(prefix=tmp_path / "opt", **kw)


def by_id(report: dict) -> dict[str, dict]:
    return {c["id"]: c for c in report["components"]}


def test_parse_components_and_peers() -> None:
    assert parse_components(None) == ("h3", "flashvsr")
    assert parse_components(" flashvsr,h3,flashvsr ") == ("flashvsr", "h3")
    with pytest.raises(ValueError, match="valid: h3, flashvsr"):
        parse_components("h3,wan")
    with pytest.raises(ValueError, match="valid"):
        parse_components(",")
    assert parse_peer("sfx-api=http://127.0.0.1:8769/v1/health") == Peer(
        "sfx-api", "http://127.0.0.1:8769/v1/health"
    )
    for bad in ("sfx-api", "sfx api=http://x/h", "x=ftp://x/h", "x=http://u:p@x/h"):
        with pytest.raises(ValueError):
            parse_peer(bad)


def test_check_changes_nothing(tmp_path: Path, blank: Settings) -> None:
    rec = Recorder()
    config = tmp_path / "none.toml"
    code, report = run_setup(blank, config, opts(tmp_path, check=True), deps(rec, tmp_path))
    assert code == 0 and report["ok"] and report["action"] == "check"
    h3, flashvsr = by_id(report)["h3"], by_id(report)["flashvsr"]
    assert h3["status"] == "missing" and flashvsr["status"] == "missing"
    assert h3["downloadBytes"] == 1_000_000_000 + 118_000_000_000
    assert flashvsr["downloadBytes"] == 1_000_000_000 + 6_800_000_000
    assert rec.calls == []
    assert not config.exists() and not (tmp_path / "opt").exists()
    json.dumps(report)  # serialisable as is


def test_check_reports_unsupported_with_reason(tmp_path: Path, blank: Settings) -> None:
    rec = Recorder()
    old = Machine(arm64=True, macos="15.7.4", ram_gb=16, free_bytes=10**12, tools={})
    _, report = run_setup(
        blank,
        tmp_path / "c.toml",
        opts(tmp_path, check=True),
        deps(rec, tmp_path, machine=lambda _: old),
    )
    h3 = by_id(report)["h3"]
    assert h3["status"] == "unsupported" and "macOS 26+" in h3["message"]
    assert h3["fix"]["needsHuman"] is True
    no_cmake = Machine(
        arm64=True, macos="26.1", ram_gb=16, free_bytes=10**12, tools={"git": True, "xcode": True}
    )
    _, report = run_setup(
        blank,
        tmp_path / "c.toml",
        opts(tmp_path, check=True),
        deps(rec, tmp_path, machine=lambda _: no_cmake),
    )
    h3 = by_id(report)["h3"]
    assert h3["status"] == "unsupported" and "cmake" in h3["message"]
    assert h3["fix"]["command"] == "brew install cmake"
    intel = Machine(arm64=False, macos="26.0", ram_gb=16, free_bytes=10**12, tools={})
    _, report = run_setup(
        blank,
        tmp_path / "c.toml",
        opts(tmp_path, check=True),
        deps(rec, tmp_path, machine=lambda _: intel),
    )
    assert "Apple Silicon" in by_id(report)["flashvsr"]["message"]


def test_install_builds_once_prepares_and_writes_config(tmp_path: Path, blank: Settings) -> None:
    rec = Recorder()
    config = tmp_path / "cfg" / "config.toml"
    code, report = run_setup(blank, config, opts(tmp_path, yes=True), deps(rec, tmp_path))
    assert code == 0 and report["ok"], report
    assert rec.calls == [
        "build vpipe",
        "prepare minimax-h3-turbo-video",
        "prepare flashvsr-upscale",
    ]
    assert {c["status"] for c in report["components"]} == {"installed"}
    data = tomllib.loads(config.read_text())
    prefix = tmp_path / "opt"
    assert data == {
        "vpipe_bin": str(binary_path(prefix / "vpipe")),
        "vpipe_src_dir": str(prefix / "vpipe"),
        "work_dir": str(prefix / "work"),
    }
    # idempotent: the second run does nothing and reports `already`
    rec2 = Recorder()
    settings = load_settings(env={}, config_path=config)
    code, report = run_setup(settings, config, opts(tmp_path), deps(rec2, tmp_path))
    assert code == 0 and rec2.calls == []
    assert {c["status"] for c in report["components"]} == {"already"}


def test_existing_vpipe_is_used_not_rebuilt(tmp_path: Path, settings: Settings) -> None:
    rec = Recorder()
    (tmp_path / "src" / "docs" / "pipelines").mkdir(parents=True)
    s = settings.model_copy(update={"vpipe_src_dir": tmp_path / "src"})
    code, report = run_setup(
        s,
        tmp_path / "c.toml",
        opts(tmp_path, components=("flashvsr",), yes=True),
        deps(rec, tmp_path),
    )
    assert code == 0 and rec.calls == ["prepare flashvsr-upscale"]
    assert by_id(report)["flashvsr"]["downloadBytes"] == 6_800_000_000
    assert tomllib.loads((tmp_path / "c.toml").read_text())["vpipe_bin"] == str(settings.vpipe_bin)


def test_without_yes_and_no_terminal_needs_confirmation(tmp_path: Path, blank: Settings) -> None:
    rec = Recorder()
    config = tmp_path / "c.toml"
    code, report = run_setup(blank, config, opts(tmp_path, components=("h3",)), deps(rec, tmp_path))
    assert code == 3 and not report["ok"] and rec.calls == []
    fix = by_id(report)["h3"]["fix"]
    assert fix["command"] == "vpipe-api setup --models h3 --yes"
    assert not config.exists()
    declined = deps(rec, tmp_path, confirm=lambda question: "GB" in question and False)
    assert run_setup(blank, config, opts(tmp_path), declined)[0] == 3
    asked: list[str] = []
    accepted = deps(rec, tmp_path, confirm=lambda q: asked.append(q) or True)
    assert run_setup(blank, config, opts(tmp_path), accepted)[0] == 0
    assert asked == ["download ~126 GB for h3, flashvsr?"]  # vpipe's clone counted once


def test_disk_check_uses_the_h3_peak(tmp_path: Path, blank: Settings) -> None:
    rec = Recorder()
    small = Machine(
        arm64=True, macos="26.0", ram_gb=32, free_bytes=190 * 10**9, tools=GOOD_MAC.tools
    )
    code, report = run_setup(
        blank,
        tmp_path / "c.toml",
        opts(tmp_path, components=("h3",), yes=True),
        deps(rec, tmp_path, machine=lambda _: small),
    )
    h3 = by_id(report)["h3"]
    assert code == 1 and h3["status"] == "failed" and rec.calls == []
    assert "needs ~201 GB free" in h3["message"]  # 185 + 1 + 5 (build) + 10 margin
    enough = Machine(
        arm64=True, macos="26.0", ram_gb=32, free_bytes=202 * 10**9, tools=GOOD_MAC.tools
    )
    code, _ = run_setup(
        blank,
        tmp_path / "c.toml",
        opts(tmp_path, components=("h3",), yes=True),
        deps(rec, tmp_path, machine=lambda _: enough),
    )
    assert code == 0


def test_failures_are_reported_per_component(tmp_path: Path, blank: Settings) -> None:
    code, report = run_setup(
        blank,
        tmp_path / "c.toml",
        opts(tmp_path, yes=True),
        deps(Recorder(fail_build=True), tmp_path),
    )
    assert code == 1 and {c["status"] for c in report["components"]} == {"failed"}
    assert "vpipe build: cmake build failed" in by_id(report)["h3"]["message"]
    assert not (tmp_path / "c.toml").exists()  # nothing built, nothing to write
    code, report = run_setup(
        blank,
        tmp_path / "c.toml",
        opts(tmp_path, yes=True),
        deps(Recorder(fail_prepare=True), tmp_path),
    )
    assert code == 1 and by_id(report)["flashvsr"]["message"] == "download did not finish"
    assert (tmp_path / "c.toml").exists()  # vpipe was built: its paths are kept


def test_unsupported_install_fails_without_work(tmp_path: Path, blank: Settings) -> None:
    rec = Recorder()
    old = Machine(arm64=True, macos="15.7", ram_gb=16, free_bytes=10**12, tools={})
    code, report = run_setup(
        blank,
        tmp_path / "c.toml",
        opts(tmp_path, yes=True),
        deps(rec, tmp_path, machine=lambda _: old),
    )
    assert code == 1 and rec.calls == [] and not report["ok"]


def test_lan_port_peers_and_service(tmp_path: Path, settings: Settings) -> None:
    install_models(settings.work_dir)  # type: ignore[arg-type]
    rec = Recorder()
    config = tmp_path / "c.toml"
    code, report = run_setup(
        settings,
        config,
        opts(
            tmp_path,
            lan=True,
            port=9001,
            service=True,
            coordinate_with=(Peer("sfx-api", "http://127.0.0.1:8769/v1/health"),),
        ),
        deps(rec, tmp_path, env={"VPIPE_API_TOKEN": TOKEN}),
    )
    assert code == 0 and report["ok"], report
    assert report["lan"] and report["tokenSet"] and report["url"] == "http://127.0.0.1:9001"
    assert rec.calls == ["service port=9001 token=True"]
    assert report["service"]["healthy"] is True
    data = tomllib.loads(config.read_text())
    assert data["host"] == "0.0.0.0" and data["port"] == 9001 and data["token"] == TOKEN
    assert data["coordinate_with"] == [
        {"name": "sfx-api", "health_url": "http://127.0.0.1:8769/v1/health"}
    ]
    assert TOKEN not in json.dumps(report)


def test_unhealthy_service_fails(tmp_path: Path, settings: Settings) -> None:
    install_models(settings.work_dir)  # type: ignore[arg-type]
    rec = Recorder()
    rec.install_service = lambda **_: ServiceState(True, "l", False)  # type: ignore[method-assign]
    code, report = run_setup(
        settings, tmp_path / "c.toml", opts(tmp_path, service=True), deps(rec, tmp_path)
    )
    assert code == 1 and not report["ok"]
