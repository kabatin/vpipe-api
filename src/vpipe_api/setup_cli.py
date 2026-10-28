"""The ``vpipe-api setup`` command line (its logic lives in ``vpipe_api.setup.command``).

Every way it can end prints the same report: with ``--json`` exactly one JSON object as the
last line of stdout, even for a usage error (exit 2) or a config it cannot read (exit 1).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from vpipe_api.settings import (
    DEFAULT_CONFIG_PATH,
    SettingsError,
    load_settings,
    resolve_config_path,
)
from vpipe_api.setup.command import (
    EXIT_BAD_ARGS,
    EXIT_FAILED,
    SetupDeps,
    build_options,
    error_payload,
    run_setup,
)
from vpipe_api.setup.components import DEFAULT_PREFIX
from vpipe_api.setup.vpipe_build import SetupError, build_vpipe


def _out(message: str = "") -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _err(message: str) -> None:
    sys.stderr.write(message + "\n")


def add_arguments(setup: argparse.ArgumentParser) -> None:
    setup.add_argument("--models", help="comma list of components: h3, flashvsr (default: all)")
    setup.add_argument("--check", action="store_true", help="change nothing, report state")
    setup.add_argument("--yes", action="store_true", help="do not ask before downloading")
    setup.add_argument("--json", action="store_true", help="one JSON object on stdout")
    setup.add_argument("--lan", action="store_true", help="listen on 0.0.0.0 with a token")
    setup.add_argument("--service", action="store_true", help="run serve as a launchd agent")
    setup.add_argument("--port", type=int)
    setup.add_argument("--prefix", default=str(DEFAULT_PREFIX), help="where vpipe/work go")
    setup.add_argument(
        "--label-suffix", help="launchd label suffix, beside another install (needs --port)"
    )
    setup.add_argument(
        "--coordinate-with",
        action="append",
        default=[],
        metavar="NAME=HEALTH_URL",
        help="another GPU server to wait for (repeatable)",
    )


def _ask(question: str) -> bool:
    _err(f"{question} [y/N] ")
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def default_deps() -> SetupDeps:
    from vpipe_api.setup.machine import inspect_machine
    from vpipe_api.setup.models import ModelPreparer

    return SetupDeps(
        machine=inspect_machine,
        build=lambda src, emit: build_vpipe(src, emit=emit),
        preparer=lambda settings, emit: ModelPreparer(settings, emit=emit),
        home=Path.home(),
        confirm=_ask if sys.stdin.isatty() else None,
        env=os.environ,
    )


def print_report(report: dict) -> None:
    _out(
        f"vpipe-api {report['version']} setup ({report['action']}): "
        f"{'ok' if report['ok'] else 'not done'}"
    )
    for comp in report["components"]:
        size = f" [{comp['downloadBytes'] / 1e9:.0f} GB]" if comp["downloadBytes"] else ""
        _out(f"  {comp['id']:<9} {comp['status']:<12} {comp['message']}{size}")
        if "fix" in comp:
            fix = comp["fix"]
            _out(
                f"            fix: {fix['hint']}"
                + (f" — {fix['command']}" if fix["command"] else "")
            )
    service = report["service"]
    _out(
        f"  config    {report['configPath']}  url {report['url']}  token "
        f"{'set' if report['tokenSet'] else 'none'}"
    )
    _out(
        f"  service   {service['label']}: "
        f"{'installed' if service['installed'] else 'not installed'}"
        + ("" if service["healthy"] is None else f", healthy: {service['healthy']}")
    )


def _config_arg(argv: Sequence[str]) -> Path | None:
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if arg.startswith("--config="):
            return Path(arg.split("=", 1)[1])
    return None


def usage_error(argv: Sequence[str], message: str) -> int:
    """argparse refused the arguments; ``setup --json`` still gets its one JSON object."""
    _err(f"error: {message}")
    if "setup" in argv and "--json" in argv:
        report = error_payload(
            message,
            action="check" if "--check" in argv else "install",
            config_path=resolve_config_path(config_path=_config_arg(argv)),
        )
        _out(json.dumps(report))
    return EXIT_BAD_ARGS


def run(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(config_path=Path(args.config) if args.config else None)

    def fail(code: int, message: str) -> int:
        _err(f"error: {message}")
        if args.json:
            report = error_payload(
                message,
                action="check" if args.check else "install",
                config_path=config_path,
                label_suffix=args.label_suffix,
            )
            _out(json.dumps(report))
        return code

    try:
        opts = build_options(
            models=args.models,
            check=args.check,
            yes=args.yes,
            lan=args.lan,
            service=args.service,
            port=args.port,
            prefix=Path(args.prefix),
            label_suffix=args.label_suffix,
            coordinate_with=args.coordinate_with,
            env=os.environ,
        )
    except ValueError as exc:
        return fail(EXIT_BAD_ARGS, str(exc))
    try:
        settings = load_settings(config_path=config_path)
        code, report = run_setup(
            settings,
            config_path,
            opts,
            default_deps(),
            custom_config=config_path != DEFAULT_CONFIG_PATH.expanduser(),
            emit=_err,
        )
    except (SettingsError, SetupError) as exc:
        return fail(EXIT_FAILED, str(exc))
    if args.json:
        _out(json.dumps(report))
    else:
        print_report(report)
    return code
