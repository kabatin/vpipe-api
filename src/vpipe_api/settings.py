"""Configuration: defaults < TOML file < ``VPIPE_API_*`` environment variables.

The TOML file is ``$VPIPE_API_CONFIG`` or ``~/.config/vpipe-api/config.toml``. Per-workflow
options live in ``[workflows."<workflow-id>"]`` tables and are validated by the workflow itself;
the other GPU servers to wait for live in ``[[coordinate_with]]`` tables (file only).
"""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

ENV_PREFIX = "VPIPE_API_"
DEFAULT_CONFIG_PATH = Path("~/.config/vpipe-api/config.toml")
DEFAULT_DATA_DIR = Path("~/.local/share/vpipe-api")
MIN_TOKEN_LENGTH = 32
_TOKEN = re.compile(rf"[\x21-\x7e]{{{MIN_TOKEN_LENGTH},}}")
_PRINTABLE = re.compile(r"[\x21-\x7e]+")  # printable ASCII, no spaces

# Scalar settings that may come from the environment (field name -> env suffix).
_ENV_FIELDS = (
    "vpipe_bin",
    "vpipe_src_dir",
    "work_dir",
    "data_dir",
    "host",
    "port",
    "token",
    "max_waiting",
    "retention_days",
    "job_timeout_factor",
    "max_body_mb",
    "ffmpeg",
    "ffprobe",
    "settle_seconds",
    "coordination_poll_seconds",
)


class SettingsError(ValueError):
    """Raised when configuration is missing or invalid (message is user-facing)."""


class PeerServer(BaseModel):
    """Another GPU server on this Mac whose ``/v1/health`` must be idle before a job starts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    health_url: str = Field(max_length=2048)
    token: SecretStr | None = None

    @field_validator("health_url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not _PRINTABLE.fullmatch(value):
            raise ValueError("health_url must be printable ASCII without spaces")
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("health_url must be an http(s) URL with a host")
        if parts.username or parts.password:
            raise ValueError("health_url must not contain credentials (use token)")
        if parts.query or parts.fragment:
            raise ValueError("health_url must not have a query or fragment (use token)")
        try:
            parts.port  # noqa: B018 - raises ValueError for a port that is not a number
        except ValueError:
            raise ValueError("health_url has an invalid port") from None
        return value

    @field_validator("token")
    @classmethod
    def _token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not _PRINTABLE.fullmatch(value.get_secret_value()):
            raise ValueError("token must be printable ASCII without spaces")
        return value


class Settings(BaseModel):
    """Validated server configuration. Immutable once loaded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vpipe_bin: Path | None = None
    vpipe_src_dir: Path | None = None
    work_dir: Path | None = None
    data_dir: Path = DEFAULT_DATA_DIR
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    token: SecretStr | None = None
    max_waiting: int = Field(default=1, ge=0, le=100)
    retention_days: int = Field(default=7, ge=1, le=365)
    job_timeout_factor: float = Field(default=3.0, ge=1.0, le=20.0)
    max_body_mb: int = Field(default=96, ge=1, le=1024)  # a 64 MB video is ~86 MB as base64
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    coordinate_with: tuple[PeerServer, ...] = ()
    settle_seconds: float = Field(default=30.0, ge=0.0, le=600.0)
    coordination_poll_seconds: float = Field(default=5.0, ge=0.05, le=300.0)
    workflows: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.token is not None and not _TOKEN.fullmatch(self.token.get_secret_value()):
            raise ValueError(
                f"token must be at least {MIN_TOKEN_LENGTH} printable ASCII characters without "
                "spaces (e.g. `openssl rand -hex 32`)"
            )
        if not is_loopback(self.host) and self.token is None:
            raise ValueError(
                f"host {self.host!r} is not a loopback address: set VPIPE_API_TOKEN "
                "so the API is not exposed without authentication"
            )
        names = [peer.name for peer in self.coordinate_with]
        if len(set(names)) != len(names):
            raise ValueError("coordinate_with names must be unique")
        return self

    @property
    def resolved_vpipe_src_dir(self) -> Path | None:
        """Source checkout; defaults to the tree the binary was built in."""
        if self.vpipe_src_dir is not None:
            return self.vpipe_src_dir
        if self.vpipe_bin is None:
            return None
        # <src>/build/apps/vpipe/vpipe
        parents = self.vpipe_bin.parents
        return parents[3] if len(parents) > 3 else None

    def require_runtime(self) -> tuple[Path, Path]:
        """vpipe binary and work directory, which ``serve`` and generation need."""
        if self.vpipe_bin is not None and self.work_dir is not None:
            return self.vpipe_bin, self.work_dir
        missing = [name for name in ("vpipe_bin", "work_dir") if getattr(self, name) is None]
        env = ", ".join(ENV_PREFIX + name.upper() for name in missing)
        raise SettingsError(
            f"missing setting(s): {', '.join(missing)} — set {env} or add them to the "
            "config file (see `vpipe-api setup vpipe`)"
        )

    def workflow_options(self, workflow_id: str) -> Mapping[str, Any]:
        return self.workflows.get(workflow_id, {})


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _expand(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("~"):
        return str(Path(value).expanduser())
    return value


def resolve_config_path(
    env: Mapping[str, str] | None = None, config_path: Path | None = None
) -> Path:
    environ = os.environ if env is None else env
    path = config_path or Path(environ.get(ENV_PREFIX + "CONFIG", str(DEFAULT_CONFIG_PATH)))
    return path.expanduser()


def load_settings(
    env: Mapping[str, str] | None = None, config_path: Path | None = None
) -> Settings:
    """Merge defaults, the TOML file and the environment, then validate."""
    environ = os.environ if env is None else env
    path = resolve_config_path(environ, config_path)

    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SettingsError(f"cannot read config file {path}: {exc}") from exc

    for field in _ENV_FIELDS:
        raw = environ.get(ENV_PREFIX + field.upper())
        if raw is not None and raw.strip() != "":
            data[field] = raw

    data = {key: _expand(value) for key, value in data.items()}
    if "data_dir" not in data:
        data["data_dir"] = str(DEFAULT_DATA_DIR.expanduser())
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors()
        )
        # from None: pydantic's error holds the raw input, tokens included
        raise SettingsError(f"invalid configuration: {problems}") from None
