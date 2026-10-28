"""``vpipe-api setup [--models h3,flashvsr] [--check] ...``: the uniform install command.

``--check`` changes nothing and reports what is installed, what is missing and what it
would download. Without it, missing components are installed (after one confirmation),
the config file gets the keys they need, and ``--service`` loads the launchd agent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from vpipe_api import __version__
from vpipe_api.settings import PeerServer, Settings, is_loopback, is_valid_token, load_settings
from vpipe_api.setup.components import (
    COMPONENTS,
    DEFAULT_PREFIX,
    Fix,
    Layout,
    Report,
    Status,
    inspect_component,
    vpipe_cost,
)
from vpipe_api.setup.config_write import TOKEN_ENV, Peer, coord_token_env, update_config
from vpipe_api.setup.machine import Machine
from vpipe_api.setup.pins import DISK_MARGIN_BYTES
from vpipe_api.setup.service import ServiceState, install_service, service_label, service_state
from vpipe_api.setup.vpipe_build import SetupError
from vpipe_api.workflows import build_registry
from vpipe_api.workflows.base import Workflow

Emit = Callable[[str], None]
EXIT_OK, EXIT_FAILED, EXIT_BAD_ARGS, EXIT_CONFIRM = 0, 1, 2, 3
LABEL_SUFFIX = re.compile(r"[A-Za-z0-9-]{1,32}")


class Preparer(Protocol):
    def prepare(self, workflow: Workflow) -> list[str]: ...


@dataclass(frozen=True)
class SetupOptions:
    components: tuple[str, ...] = tuple(COMPONENTS)
    explicit: bool = False  # --models named them: unsupported is then a failure
    check: bool = False
    yes: bool = False
    lan: bool = False
    service: bool = False
    port: int | None = None
    prefix: Path = DEFAULT_PREFIX
    label_suffix: str | None = None
    coordinate_with: tuple[Peer, ...] = ()


@dataclass(frozen=True)
class SetupDeps:
    """Everything that touches the machine, so tests can swap it out."""

    machine: Callable[[Path], Machine]
    build: Callable[[Path, Emit], Path]
    preparer: Callable[[Settings, Emit], Preparer]
    home: Path
    # None when there is nobody to ask (stdin is not a terminal)
    confirm: Callable[[str], bool] | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    install_service: Callable[..., ServiceState] = install_service
    service_state: Callable[..., ServiceState] = service_state


def parse_components(text: str | None) -> tuple[str, ...]:
    if text is None:
        return tuple(COMPONENTS)
    wanted = tuple(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))
    unknown = [name for name in wanted if name not in COMPONENTS]
    if unknown or not wanted:
        raise ValueError(
            f"unknown component(s) {', '.join(unknown) or '(none)'}; valid: {', '.join(COMPONENTS)}"
        )
    return wanted


def parse_peer(text: str, env: Mapping[str, str] | None = None) -> Peer:
    """``NAME=HEALTH_URL``, checked with the same rules as the config, including the peer's
    token from the environment (which never appears in the error)."""
    name, sep, url = text.partition("=")
    if not sep:
        raise ValueError(f"--coordinate-with wants NAME=HEALTH_URL, got {text!r}")
    token = (env or {}).get(coord_token_env(name), "").strip() or None
    try:
        peer = PeerServer(name=name, health_url=url, token=token)  # pyright: ignore[reportArgumentType]
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ValueError(f"--coordinate-with {name}: {problems}") from None
    return Peer(peer.name, peer.health_url)


def build_options(
    *,
    models: str | None,
    check: bool = False,
    yes: bool = False,
    lan: bool = False,
    service: bool = False,
    port: int | None = None,
    prefix: Path = DEFAULT_PREFIX,
    label_suffix: str | None = None,
    coordinate_with: Sequence[str] = (),
    env: Mapping[str, str],
) -> SetupOptions:
    """Every argument checked before anything runs: a ValueError here is exit 2."""
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"--port must be 1-65535, got {port}")
    if label_suffix is not None:
        if not LABEL_SUFFIX.fullmatch(label_suffix):
            raise ValueError("--label-suffix must be 1-32 letters, digits or '-'")
        if port is None:
            raise ValueError("--label-suffix needs --port (the other install keeps its port)")
    token = env.get(TOKEN_ENV, "").strip()
    if lan and token and not is_valid_token(token):
        raise ValueError(
            f"{TOKEN_ENV} must be at least 32 printable ASCII characters without spaces"
        )
    return SetupOptions(
        components=parse_components(models),
        explicit=models is not None,
        check=check,
        yes=yes,
        lan=lan,
        service=service,
        port=port,
        prefix=prefix,
        label_suffix=label_suffix,
        coordinate_with=tuple(parse_peer(text, env) for text in coordinate_with),
    )


def _gb(n: int) -> str:
    return f"{n / 1e9:.0f} GB"


def _payload(
    action: str,
    ok: bool,
    settings: Settings,
    config_path: Path,
    machine: Machine,
    service: ServiceState,
    reports: Sequence[Report],
) -> dict[str, Any]:
    host = "127.0.0.1" if not is_loopback(settings.host) else settings.host
    return {
        "server": "vpipe-api",
        "version": __version__,
        "ok": ok,
        "action": action,
        "url": f"http://{host}:{settings.port}",
        "lan": not is_loopback(settings.host),
        "tokenSet": settings.token is not None,
        "configPath": str(config_path),
        "service": service.to_json(),
        "machine": machine.to_json(),
        "components": [report.to_json() for report in reports],
    }


def error_payload(
    error: str, *, action: str, config_path: Path, label_suffix: str | None = None
) -> dict[str, Any]:
    """The report's shape when setup could not get as far as looking at the machine."""
    return {
        "server": "vpipe-api",
        "version": __version__,
        "ok": False,
        "action": action,
        "url": None,
        "lan": False,
        "tokenSet": False,
        "configPath": str(config_path),
        "service": {"installed": False, "label": service_label(label_suffix), "healthy": None},
        "machine": None,
        "components": [],
        "error": error,
    }


def _token(settings: Settings) -> str | None:
    return settings.token.get_secret_value() if settings.token is not None else None


def _install(
    todo: Sequence[Report],
    layout: Layout,
    settings: Settings,
    deps: SetupDeps,
    emit: Emit,
) -> dict[str, Report]:
    done: dict[str, Report] = {}
    if not layout.vpipe_built:
        try:
            deps.build(layout.vpipe_src, emit)
        except SetupError as exc:
            return {
                r.id: replace(r, status=Status.FAILED, message=f"vpipe build: {exc}") for r in todo
            }
    layout.work_dir.mkdir(parents=True, exist_ok=True)
    run_settings = layout.settings(settings)
    registry = build_registry(run_settings)
    for report in todo:
        workflow = registry.get(COMPONENTS[report.id])
        assert workflow is not None  # noqa: S101 - every component's workflow is built in
        try:
            prepared = deps.preparer(run_settings, emit).prepare(workflow)
        except SetupError as exc:
            done[report.id] = replace(report, status=Status.FAILED, message=str(exc))
            continue
        message = f"prepared {', '.join(prepared)}" if prepared else "vpipe built"
        done[report.id] = replace(report, status=Status.INSTALLED, message=message)
    return done


def _gate(
    todo: Sequence[Report], layout: Layout, machine: Machine, opts: SetupOptions, deps: SetupDeps
) -> tuple[int, list[Report]] | None:
    """Disk check and confirmation: None to go ahead, else the exit code and the reports."""
    build_download, build_disk = vpipe_cost(layout)
    extra = len(todo) - 1  # every todo report counts the vpipe build; it happens once
    download = sum(r.download_bytes for r in todo) - build_download * extra
    disk = sum(r.disk_bytes for r in todo) - build_disk * extra + DISK_MARGIN_BYTES
    if machine.free_bytes < disk:
        message = (
            f"needs ~{_gb(disk)} free (incl. {_gb(DISK_MARGIN_BYTES)} margin), "
            f"only {_gb(machine.free_bytes)} free at {layout.work_dir}"
        )
        fix = Fix("free up disk space or pass --prefix on a larger volume", needs_human=True)
        return EXIT_FAILED, [
            replace(r, status=Status.FAILED, message=message, fix=fix) for r in todo
        ]
    if opts.yes:
        return None
    question = f"download ~{_gb(download)} for {', '.join(r.id for r in todo)}?"
    if deps.confirm is not None and deps.confirm(question):
        return None
    names = ",".join(r.id for r in todo)
    fix = Fix(
        f"confirm the ~{_gb(download)} download",
        command=f"vpipe-api setup --models {names} --yes",
    )
    return EXIT_CONFIRM, [replace(r, fix=fix) for r in todo]


def run_setup(
    settings: Settings,
    config_path: Path,
    opts: SetupOptions,
    deps: SetupDeps,
    *,
    custom_config: bool = False,
    emit: Emit = lambda _: None,
) -> tuple[int, dict[str, Any]]:
    """Returns the exit code and the JSON report. ``custom_config``: the config path is not
    the default one, so the service must be told about it."""
    prefix = opts.prefix.expanduser()
    layout = Layout.resolve(settings, prefix)
    machine = deps.machine(layout.work_dir)
    registry = build_registry(settings)
    reports = [inspect_component(c, layout, machine, registry) for c in opts.components]

    def state(current: Settings) -> ServiceState:
        return deps.service_state(opts.label_suffix, current.port, _token(current), home=deps.home)

    if opts.check:
        return EXIT_OK, _payload(
            "check", True, settings, config_path, machine, state(settings), reports
        )

    results = {
        r.id: replace(r, status=Status.ALREADY) if r.status is Status.INSTALLED else r
        for r in reports
    }
    todo = [r for r in reports if r.status is Status.MISSING]
    code = EXIT_OK
    if todo:
        gate = _gate(todo, layout, machine, opts, deps)
        if gate is not None:
            code, held = gate
            results |= {r.id: r for r in held}
        else:
            for report in todo:
                emit(f"{report.id}: {report.message}")
            results |= _install(todo, layout, settings, deps, emit)

    final = settings
    add = layout.config_keys() if layout.vpipe_built else {}
    if code != EXIT_CONFIRM and (add or opts.lan or opts.port or opts.coordinate_with):
        written = update_config(
            config_path,
            add=add,
            lan=opts.lan,
            port=opts.port,
            coordinate_with=opts.coordinate_with,
            env=deps.env,
        )
        emit(f"config {'updated' if written.changed else 'unchanged'}: {config_path}")
        final = load_settings(env=deps.env, config_path=config_path)

    service = state(final)
    if opts.service and code != EXIT_CONFIRM:
        service = deps.install_service(
            port=final.port,
            token=_token(final),
            suffix=opts.label_suffix,
            config=config_path if custom_config else None,
            home=deps.home,
            emit=emit,
        )

    ordered = [results[c] for c in opts.components]
    # Without --models, a component this Mac cannot run is reported, not a failure
    fine = {Status.INSTALLED, Status.ALREADY} | (set() if opts.explicit else {Status.UNSUPPORTED})
    ok = all(r.status in fine for r in ordered)
    ok = ok and (not opts.service or bool(service.healthy)) and code == EXIT_OK
    if code == EXIT_OK and not ok:
        code = EXIT_FAILED
    return code, _payload("install", ok, final, config_path, machine, service, ordered)
