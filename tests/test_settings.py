from pathlib import Path

import pytest

from vpipe_api.settings import SettingsError, is_loopback, load_settings


def test_defaults_without_file_or_env(tmp_path: Path) -> None:
    s = load_settings(env={}, config_path=tmp_path / "none.toml")
    assert s.host == "127.0.0.1"
    assert s.port == 8765
    assert s.token is None
    assert s.max_waiting == 1
    assert s.vpipe_bin is None


def test_env_overrides_file(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        'port = 9000\nmax_waiting = 3\nvpipe_bin = "~/vpipe/build/apps/vpipe/vpipe"\n'
        '[workflows."minimax-h3-turbo-video"]\nsol_attn = false\n'
    )
    s = load_settings(env={"VPIPE_API_PORT": "9100"}, config_path=cfg)
    assert s.port == 9100
    assert s.max_waiting == 3
    assert s.vpipe_bin == Path("~/vpipe/build/apps/vpipe/vpipe").expanduser()
    assert s.workflow_options("minimax-h3-turbo-video") == {"sol_attn": False}
    assert s.workflow_options("other") == {}
    assert s.resolved_vpipe_src_dir == Path("~/vpipe").expanduser()


def test_config_path_from_env(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text("port = 9001\n")
    assert load_settings(env={"VPIPE_API_CONFIG": str(cfg)}).port == 9001


def test_blank_env_values_are_ignored(tmp_path: Path) -> None:
    s = load_settings(env={"VPIPE_API_TOKEN": "  "}, config_path=tmp_path / "x.toml")
    assert s.token is None


def test_non_loopback_requires_token(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="VPIPE_API_TOKEN"):
        load_settings(env={"VPIPE_API_HOST": "0.0.0.0"}, config_path=tmp_path / "x.toml")
    s = load_settings(
        env={
            "VPIPE_API_HOST": "0.0.0.0",
            "VPIPE_API_TOKEN": "test-token-0123456789-abcdefghijklmnop",
        },
        config_path=tmp_path / "x.toml",
    )
    assert (
        s.token is not None
        and s.token.get_secret_value() == "test-token-0123456789-abcdefghijklmnop"
    )


def test_invalid_values_are_reported(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="port"):
        load_settings(env={"VPIPE_API_PORT": "70000"}, config_path=tmp_path / "x.toml")


def test_broken_toml(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text("port = = 1")
    with pytest.raises(SettingsError, match="cannot read"):
        load_settings(env={}, config_path=cfg)


def test_require_runtime(tmp_path: Path) -> None:
    s = load_settings(env={}, config_path=tmp_path / "x.toml")
    with pytest.raises(SettingsError, match="VPIPE_API_VPIPE_BIN"):
        s.require_runtime()
    ok = load_settings(
        env={"VPIPE_API_VPIPE_BIN": "/a/b", "VPIPE_API_WORK_DIR": "/w"},
        config_path=tmp_path / "x.toml",
    )
    assert ok.require_runtime() == (Path("/a/b"), Path("/w"))


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("192.168.0.5", False),
        ("example.com", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected
