"""Merge what ``setup`` learned into the config file ``settings.py`` reads.

Every existing key and value is kept; only missing keys are added (``--lan`` and ``--port``
set ``host`` / ``port`` because they were asked for). The file is written atomically, mode
0600, and only when something changed. The token is never printed or returned.
"""

from __future__ import annotations

import os
import re
import secrets
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vpipe_api.settings import (
    ENV_PREFIX,
    SettingsError,
    is_valid_token,
    validate_settings_data,
)
from vpipe_api.setup.toml_emit import dumps
from vpipe_api.setup.vpipe_build import SetupError

LAN_HOST = "0.0.0.0"  # noqa: S104 - --lan asks for exactly this
TOKEN_ENV = ENV_PREFIX + "TOKEN"
COORD_TOKEN_ENV = ENV_PREFIX + "COORD_TOKEN_"


@dataclass(frozen=True)
class Peer:
    name: str
    health_url: str


@dataclass(frozen=True)
class ConfigResult:
    path: Path
    changed: bool
    host: str
    port: int
    token_set: bool


def coord_token_env(name: str) -> str:
    return COORD_TOKEN_ENV + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SetupError(f"cannot read config file {path}: {exc}") from exc


def _lan_token(existing: Mapping[str, Any], env: Mapping[str, str]) -> str:
    from_env = env.get(TOKEN_ENV, "").strip()
    if from_env:
        if not is_valid_token(from_env):
            raise SetupError(
                f"{TOKEN_ENV} must be at least 32 printable ASCII characters without spaces"
            )
        return from_env
    current = existing.get("token")
    if isinstance(current, str) and current:
        return current
    return secrets.token_urlsafe(32)


def _peers(
    existing: Sequence[Mapping[str, Any]], wanted: Sequence[Peer], env: Mapping[str, str]
) -> list[dict[str, Any]]:
    merged = [dict(peer) for peer in existing]
    names = {peer.get("name") for peer in merged}
    for peer in wanted:
        if peer.name in names:
            continue
        entry: dict[str, Any] = {"name": peer.name, "health_url": peer.health_url}
        token = env.get(coord_token_env(peer.name), "").strip()
        if token:
            entry["token"] = token
        merged.append(entry)
        names.add(peer.name)
    return merged


def merge_config(
    existing: Mapping[str, Any],
    *,
    add: Mapping[str, Any],
    lan: bool = False,
    port: int | None = None,
    coordinate_with: Sequence[Peer] = (),
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The new file contents: ``existing`` plus whatever it lacks. Never removes a key."""
    environ = os.environ if env is None else env
    merged = dict(existing) | {k: v for k, v in add.items() if k not in existing}
    if lan:
        merged = merged | {"host": LAN_HOST, "token": _lan_token(existing, environ)}
    if port is not None:
        merged = merged | {"port": port}
    if coordinate_with:
        merged = merged | {
            "coordinate_with": _peers(existing.get("coordinate_with", []), coordinate_with, environ)
        }
    return merged


def write_atomic(path: Path, text: str) -> None:
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        tmp.chmod(0o600)  # the umask may have narrowed os.open's mode, never widened it
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def update_config(
    path: Path,
    *,
    add: Mapping[str, Any],
    lan: bool = False,
    port: int | None = None,
    coordinate_with: Sequence[Peer] = (),
    env: Mapping[str, str] | None = None,
) -> ConfigResult:
    existing = read_config(path)
    merged = merge_config(
        existing, add=add, lan=lan, port=port, coordinate_with=coordinate_with, env=env
    )
    text = dumps(merged)
    try:
        settings = validate_settings_data(tomllib.loads(text))
    except SettingsError as exc:
        raise SetupError(f"the config setup would write is not valid: {exc}") from None
    changed = merged != existing
    if changed:
        write_atomic(path, text)
    return ConfigResult(
        path=path,
        changed=changed,
        host=settings.host,
        port=settings.port,
        token_set=settings.token is not None,
    )
