import plistlib
from pathlib import Path

import pytest

from vpipe_api.setup import service
from vpipe_api.setup.machine import Machine, existing_parent, inspect_machine
from vpipe_api.setup.vpipe_build import SetupError


def _install(tmp_path: Path, calls: list, bootstrap_rc: int = 0, **kw):
    def run(cmd):
        calls.append(list(cmd))
        return bootstrap_rc if cmd[1] == "bootstrap" else 3  # bootout: not loaded

    return service.install_service(
        port=8765,
        token=kw.pop("token", None),
        suffix=kw.pop("suffix", None),
        config=kw.pop("config", None),
        home=tmp_path,
        run=run,
        which=lambda _: "/Users/x/.local/bin/vpipe-api",
        sleep=lambda _: None,
        **kw,
    )


def test_service_writes_plist_reloads_and_waits_for_health(tmp_path: Path) -> None:
    calls: list = []
    answers = iter([False, False, True])
    seen: list = []

    def health(url, token):
        seen.append((url, token))
        return next(answers)

    state = _install(
        tmp_path, calls, suffix="test", token="t" * 40, config=tmp_path / "c.toml", health=health
    )
    label = "com.github.kabatin.vpipe-api.test"
    assert state == service.ServiceState(True, label, True)
    plist = plistlib.loads((tmp_path / "Library/LaunchAgents" / f"{label}.plist").read_bytes())
    assert plist["ProgramArguments"] == ["/Users/x/.local/bin/vpipe-api", "serve"]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["StandardOutPath"] == str(tmp_path / "Library/Logs/vpipe-api.test.log")
    assert plist["EnvironmentVariables"]["VPIPE_API_CONFIG"] == str(tmp_path / "c.toml")
    assert "t" * 40 not in str(plist)  # the token stays in the config file
    assert calls[0][:2] == ["launchctl", "bootout"] and calls[0][2].endswith(f"/{label}")
    assert calls[1][:2] == ["launchctl", "bootstrap"]
    assert seen[0] == ("http://127.0.0.1:8765/v1/health", "t" * 40)


def test_service_default_config_and_failures(tmp_path: Path) -> None:
    calls: list = []
    state = _install(tmp_path, calls, health=lambda *_: False, timeout_s=0)
    assert state.healthy is False and state.label == "com.github.kabatin.vpipe-api"
    plist = plistlib.loads(
        (tmp_path / "Library/LaunchAgents/com.github.kabatin.vpipe-api.plist").read_bytes()
    )
    assert "VPIPE_API_CONFIG" not in plist["EnvironmentVariables"]
    with pytest.raises(SetupError, match="bootstrap"):
        _install(tmp_path, [], bootstrap_rc=5, health=lambda *_: True)
    with pytest.raises(SetupError, match="not on PATH"):
        service.install_service(
            port=1, token=None, suffix=None, config=None, home=tmp_path, which=lambda _: None
        )


def test_service_state(tmp_path: Path) -> None:
    absent = service.service_state(None, 8765, None, home=tmp_path, health=lambda *_: True)
    assert absent.to_json() == {"installed": False, "label": service.LABEL, "healthy": None}
    _install(tmp_path, [], health=lambda *_: True)
    present = service.service_state(None, 8765, None, home=tmp_path, health=lambda *_: True)
    assert present.installed and present.healthy


def test_is_healthy_against_nothing() -> None:
    assert service.is_healthy("http://127.0.0.1:9/v1/health", None) is False


def test_inspect_machine(tmp_path: Path) -> None:
    answers = {"sysctl -n hw.memsize": "34359738368\n", "xcodebuild -version": None}
    machine = inspect_machine(
        tmp_path / "not" / "yet",
        probe=lambda cmd: answers.get(" ".join(cmd), "1"),
        has_tool=lambda name: name != "cmake",
    )
    assert machine.ram_gb == 32 and machine.free_bytes > 0
    assert machine.tools["cmake"] is False and machine.tools["xcode"] is False
    assert existing_parent(tmp_path / "a" / "b") == tmp_path


def test_machine_reasons() -> None:
    ok = Machine(True, "26.0.1", 32, 1, {"xcode": True, "cmake": True, "git": True})
    assert ok.vpipe_unsupported() is None and ok.build_unsupported() is None
    assert "macOS 26+" in (Machine(True, "15.7", 32, 1).vpipe_unsupported() or "")
    assert "macOS ?" in (Machine(True, "", 32, 1).vpipe_unsupported() or "")
    assert "Xcode" not in (Machine(True, "26.0", 32, 1, {}).build_unsupported() or "")
    assert Machine(True, "26.0", 32, 1, {}).build_unsupported() == (
        "building vpipe needs xcode, cmake, git"
    )
