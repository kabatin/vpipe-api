import stat
import tomllib
from pathlib import Path

import pytest

from vpipe_api.setup.config_write import (
    LAN_HOST,
    Peer,
    coord_token_env,
    merge_config,
    update_config,
)
from vpipe_api.setup.toml_emit import dumps
from vpipe_api.setup.vpipe_build import SetupError

TOKEN = "env-token-0123456789-abcdefghijklmnop"


def test_toml_round_trip() -> None:
    data = {
        "vpipe_bin": '/p/with "quotes" and \\ back\tslash',
        "port": 8765,
        "job_timeout_factor": 3.5,
        "on": True,
        "off": False,
        "names": ["a", "b c", "ü"],
        "empty": [],
        "control": "a\x01b\x7f",
        "workflows": {
            "minimax-h3-turbo-video": {"model_key": "local/x", "steps": 4},
            "with space": {"k": "v"},
        },
        "coordinate_with": [
            {"name": "ace-step-api", "health_url": "http://127.0.0.1:8768/v1/health"},
            {"name": "sfx-api", "health_url": "http://127.0.0.1:8769/v1/health", "token": "t"},
        ],
    }
    assert tomllib.loads(dumps(data)) == data


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), None, [1, 2], {"a": [{"b": {"c": 1}}]}]
)
def test_toml_refuses_what_it_cannot_write(value: object) -> None:
    with pytest.raises(TypeError):
        dumps({"x": value})


def test_merge_keeps_existing_values_and_adds_missing_keys() -> None:
    existing = {"vpipe_bin": "/mine", "max_waiting": 3, "workflows": {"w": {"a": 1}}}
    merged = merge_config(existing, add={"vpipe_bin": "/new", "work_dir": "/w"}, env={})
    assert merged == existing | {"work_dir": "/w"}
    assert existing == {"vpipe_bin": "/mine", "max_waiting": 3, "workflows": {"w": {"a": 1}}}


def test_lan_token_order_env_then_existing_then_generated() -> None:
    from_env = merge_config({"token": "x" * 40}, add={}, lan=True, env={"VPIPE_API_TOKEN": TOKEN})
    assert from_env["host"] == LAN_HOST and from_env["token"] == TOKEN
    kept = merge_config({"token": "y" * 40}, add={}, lan=True, env={})
    assert kept["token"] == "y" * 40
    generated = merge_config({}, add={}, lan=True, env={})
    assert len(generated["token"]) >= 32
    with pytest.raises(SetupError, match="32 printable") as info:
        merge_config({}, add={}, lan=True, env={"VPIPE_API_TOKEN": "short secret"})
    assert "short secret" not in str(info.value)


def test_coordinate_with_dedupes_by_name_and_reads_peer_tokens() -> None:
    existing = {"coordinate_with": [{"name": "ace-step-api", "health_url": "http://a/v1/health"}]}
    merged = merge_config(
        existing,
        add={},
        coordinate_with=(
            Peer("ace-step-api", "http://other/v1/health"),
            Peer("sfx-api", "http://127.0.0.1:8769/v1/health"),
            Peer("sfx-api", "http://dup/v1/health"),
        ),
        env={coord_token_env("sfx-api"): "peer-token"},
    )
    assert merged["coordinate_with"] == [
        {"name": "ace-step-api", "health_url": "http://a/v1/health"},
        {"name": "sfx-api", "health_url": "http://127.0.0.1:8769/v1/health", "token": "peer-token"},
    ]
    assert coord_token_env("klein.api-2") == "VPIPE_API_COORD_TOKEN_KLEIN_API_2"


def test_update_writes_atomically_0600_and_only_on_change(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "config.toml"
    first = update_config(path, add={"work_dir": "/w"}, port=9000, env={})
    assert first.changed and first.port == 9000 and not first.token_set
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert tomllib.loads(path.read_text()) == {"work_dir": "/w", "port": 9000}
    before = path.stat().st_mtime_ns
    again = update_config(path, add={"work_dir": "/other"}, env={})
    assert not again.changed and path.stat().st_mtime_ns == before
    assert list(path.parent.iterdir()) == [path]  # no temp file left behind


def test_update_keeps_a_hand_written_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'vpipe_bin = "/x"\nretention_days = 3\n\n[workflows."flashvsr-upscale"]\nfoo = 1\n'
    )
    result = update_config(path, add={"vpipe_bin": "/y", "work_dir": "/w"}, lan=True, env={})
    data = tomllib.loads(path.read_text())
    assert data["vpipe_bin"] == "/x" and data["retention_days"] == 3
    assert data["workflows"] == {"flashvsr-upscale": {"foo": 1}}
    assert data["host"] == LAN_HOST and result.token_set and result.host == LAN_HOST


def test_update_refuses_an_invalid_result_and_leaves_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("port = 8765\n")
    with pytest.raises(SetupError, match="not valid"):
        update_config(path, add={}, port=70000, env={})
    assert path.read_text() == "port = 8765\n"
    path.write_text("not [toml")
    with pytest.raises(SetupError, match="cannot read"):
        update_config(path, add={}, env={})
