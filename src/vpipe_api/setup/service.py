"""``setup --service``: run ``vpipe-api serve`` as a launchd agent that starts at login."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from vpipe_api.jobs.coordination import PeerUnreachableError, probe_health
from vpipe_api.settings import ENV_PREFIX, PeerServer
from vpipe_api.setup.fs import make_private_dirs, write_atomic
from vpipe_api.setup.vpipe_build import SetupError

LABEL = "com.github.kabatin.vpipe-api"
HEALTH_TIMEOUT_S = 60.0
UNLOAD_WAIT_S = 10.0
POLL_S = 0.5
# launchctl bootstrap: 5 (EIO) while the old agent is still going away, 37 (EALREADY)
BOOTSTRAP_RETRY_CODES = frozenset({5, 37})
BOOTSTRAP_TRIES = 5
# launchd starts agents with a bare PATH; ffmpeg usually lives in Homebrew's
SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

Run = Callable[[Sequence[str]], int]
Health = Callable[[str, str | None], bool]


@dataclass(frozen=True)
class ServiceState:
    installed: bool
    label: str
    healthy: bool | None

    def to_json(self) -> dict[str, object]:
        return {"installed": self.installed, "label": self.label, "healthy": self.healthy}


def service_label(suffix: str | None) -> str:
    return f"{LABEL}.{suffix}" if suffix else LABEL


def plist_path(label: str, home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


def log_path(label: str, home: Path) -> Path:
    return home / "Library" / "Logs" / f"{label.removeprefix('com.github.kabatin.')}.log"


def health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/v1/health"


def is_healthy(url: str, token: str | None) -> bool:
    peer = PeerServer(
        name="self", health_url=url, token=SecretStr(token) if token is not None else None
    )
    try:
        probe_health(peer, timeout_s=2.0)
    except PeerUnreachableError:
        return False
    return True


def build_plist(cli: Path, label: str, log: Path, config: Path | None) -> dict[str, object]:
    env = {"PATH": SERVICE_PATH}
    if config is not None:
        env[ENV_PREFIX + "CONFIG"] = str(config)
    return {
        "Label": label,
        "ProgramArguments": [str(cli), "serve"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after a clean stop
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": env,
        "ProcessType": "Interactive",
    }


def _default_run(cmd: Sequence[str]) -> int:
    return subprocess.run(list(cmd), capture_output=True, check=False).returncode


def service_state(
    suffix: str | None, port: int, token: str | None, *, home: Path, health: Health = is_healthy
) -> ServiceState:
    label = service_label(suffix)
    installed = plist_path(label, home).is_file()
    return ServiceState(installed, label, health(health_url(port), token) if installed else None)


def install_service(
    *,
    port: int,
    token: str | None,
    suffix: str | None,
    config: Path | None,
    home: Path,
    run: Run = _default_run,
    which: Callable[[str], str | None] = shutil.which,
    health: Health = is_healthy,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: float = HEALTH_TIMEOUT_S,
    emit: Callable[[str], None] = lambda _: None,
) -> ServiceState:
    """Write the plist, (re)load it, and wait for ``/v1/health``. ``config`` is the config
    file to pass on, None for the default one."""
    found = which("vpipe-api")
    if found is None:
        raise SetupError("vpipe-api is not on PATH — install it with `uv tool install`")
    label = service_label(suffix)
    plist = plist_path(label, home)
    log = log_path(label, home)
    make_private_dirs(log.parent)
    data = build_plist(Path(found).absolute(), label, log, config)
    write_atomic(plist, plistlib.dumps(data), mode=0o644)
    domain = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", f"{domain}/{label}"])  # not loaded yet is fine
    # bootout returns before the old agent is gone; bootstrapping over it fails with EIO
    for _ in range(int(UNLOAD_WAIT_S / POLL_S)):
        if run(["launchctl", "print", f"{domain}/{label}"]) != 0:
            break
        sleep(POLL_S)
    for attempt in range(BOOTSTRAP_TRIES):
        code = run(["launchctl", "bootstrap", domain, str(plist)])
        if code == 0:
            break
        if code not in BOOTSTRAP_RETRY_CODES or attempt == BOOTSTRAP_TRIES - 1:
            raise SetupError(f"launchctl bootstrap {domain} {plist} failed (exit {code})")
        sleep(1.0)
    emit(f"service {label} loaded; waiting for {health_url(port)}")
    deadline = time.monotonic() + timeout_s
    while True:
        if health(health_url(port), token):
            return ServiceState(True, label, True)
        if time.monotonic() >= deadline:
            emit(f"service did not answer within {timeout_s:.0f}s — see {log}")
            return ServiceState(True, label, False)
        sleep(1.0)
