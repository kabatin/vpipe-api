"""``vpipe-api`` command line: serve, doctor, workflows, config, setup."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from vpipe_api import __version__
from vpipe_api.doctor import Check, Level, run_doctor
from vpipe_api.settings import Settings, SettingsError, load_settings, resolve_config_path
from vpipe_api.setup.vpipe_build import DEFAULT_TAG, SetupError, build_vpipe
from vpipe_api.workflows import build_registry

_MARKS = {Level.OK: "✓", Level.WARN: "!", Level.FAIL: "✗"}


def _out(message: str = "") -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _err(message: str) -> None:
    sys.stderr.write(message + "\n")


def _print_check(check: Check) -> None:
    _out(f"  {_MARKS[check.level]} {check.name:<50} {check.detail}")


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    from vpipe_api.api.app import create_app
    from vpipe_api.jobs.queue import JobQueue
    from vpipe_api.jobs.store import JobStore, StoreLockedError
    from vpipe_api.runner import VpipeRunner

    if args.host or args.port:
        settings = Settings.model_validate(
            settings.model_dump()
            | {k: v for k, v in (("host", args.host), ("port", args.port)) if v}
        )
    vpipe_bin, work_dir = settings.require_runtime()
    store = JobStore(settings.data_dir)
    try:
        store.acquire_instance_lock()
    except StoreLockedError as exc:
        _err(f"error: {exc}")
        return 1
    registry = build_registry(settings)
    queue = JobQueue(
        store,
        registry,
        VpipeRunner(vpipe_bin, work_dir),
        max_waiting=settings.max_waiting,
        timeout_factor=settings.job_timeout_factor,
        retention_days=settings.retention_days,
    )
    token = settings.token.get_secret_value() if settings.token else None
    app = create_app(queue, registry, token=token, max_body_bytes=settings.max_body_mb << 20)
    _out(
        f"vpipe-api {__version__} on http://{settings.host}:{settings.port} "
        f"(auth: {'bearer' if token else 'none'}, workflows: "
        f"{', '.join(w.id for w in registry)})"
    )
    try:
        uvicorn.run(app, host=settings.host, port=settings.port, workers=1, log_level="info")
    finally:
        store.release_instance_lock()
    return 0


def cmd_doctor(settings: Settings, args: argparse.Namespace) -> int:
    _out(f"vpipe-api {__version__} doctor")
    checks = run_doctor(
        settings,
        build_registry(settings),
        smoke=args.smoke,
        emit=_print_check,
        config_path=resolve_config_path(config_path=Path(args.config) if args.config else None),
    )
    failed = [c for c in checks if c.level is Level.FAIL]
    _out(f"\n{len(failed)} problem(s)" if failed else "\nall good")
    return 1 if failed else 0


def cmd_workflows(settings: Settings, _: argparse.Namespace) -> int:
    registry = build_registry(settings)
    for workflow in registry:
        _out(f"{workflow.id}\n  {workflow.description}")
        for model in workflow.required_models:
            state = (
                "present"
                if settings.work_dir and model.is_present(settings.work_dir)
                else "missing"
            )
            _out(f"  - {model.key}: {state}")
    return 0


def cmd_config(settings: Settings, _: argparse.Namespace) -> int:
    for key, value in settings.model_dump(exclude={"workflows"}).items():
        shown = "********" if key == "token" and value is not None else value
        _out(f"{key} = {shown}")
    for workflow_id, options in settings.workflows.items():
        _out(f"[workflows.{workflow_id!r}] {options}")
    return 0


def cmd_setup_vpipe(_: Settings, args: argparse.Namespace) -> int:
    src = Path(args.dir).expanduser().resolve()
    binary = build_vpipe(src, tag=args.tag, emit=_out)
    work = Path(args.work_dir).expanduser().resolve() if args.work_dir else src.parent / "work"
    work.mkdir(parents=True, exist_ok=True)
    _out("\nbuilt. Add to ~/.config/vpipe-api/config.toml:\n")
    _out(f'vpipe_bin = "{binary}"\nwork_dir = "{work}"')
    return 0


def cmd_setup_models(settings: Settings, args: argparse.Namespace) -> int:
    from vpipe_api.setup.models import ModelPreparer

    workflow = build_registry(settings).get(args.workflow)
    if workflow is None:
        _err(f"error: unknown workflow {args.workflow!r}")
        return 2
    missing = [
        m
        for m in workflow.required_models
        if settings.work_dir is None or not m.is_present(settings.work_dir)
    ]
    if not missing:
        _out("all models present")
        return 0
    need = max(m.disk_gb_needed for m in missing)
    _out(f"will prepare: {', '.join(m.key for m in missing)} (peak ~{need} GB disk, hours)")
    if not args.yes and input("continue? [y/N] ").strip().lower() != "y":
        return 1
    prepared = ModelPreparer(settings, emit=_out).prepare(workflow)
    _out(f"done: {', '.join(prepared) or 'nothing to do'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vpipe-api", description=__doc__)
    parser.add_argument("--version", action="version", version=f"vpipe-api {__version__}")
    parser.add_argument("--config", help="config file (default ~/.config/vpipe-api/config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    doctor = sub.add_parser("doctor", help="check this machine")
    doctor.add_argument("--smoke", action="store_true", help="also run one tiny generation")
    doctor.set_defaults(func=cmd_doctor)

    sub.add_parser("workflows", help="list workflows and their models").set_defaults(
        func=cmd_workflows
    )
    sub.add_parser("config", help="show the effective configuration").set_defaults(func=cmd_config)

    setup = sub.add_parser("setup", help="install vpipe or models")
    setup_sub = setup.add_subparsers(dest="target", required=True)
    vpipe = setup_sub.add_parser("vpipe", help="clone and build vpipe")
    vpipe.add_argument("--dir", required=True, help="where to clone vpipe")
    vpipe.add_argument("--tag", default=DEFAULT_TAG)
    vpipe.add_argument("--work-dir", help="vpipe work directory (default: <dir>/../work)")
    vpipe.set_defaults(func=cmd_setup_vpipe)
    models = setup_sub.add_parser("models", help="download/prepare a workflow's models")
    models.add_argument("workflow")
    models.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    models.set_defaults(func=cmd_setup_models)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings(config_path=Path(args.config) if args.config else None)
        return int(args.func(settings, args))
    except (SettingsError, SetupError) as exc:
        _err(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
