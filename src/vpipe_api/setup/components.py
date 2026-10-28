"""What ``setup --models`` can install, and how each one stands on this Mac.

``h3`` is vpipe itself plus MiniMax H3 Turbo; ``flashvsr`` is the FlashVSR upscaler, which
also runs on vpipe, so either one builds vpipe when it is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vpipe_api.settings import Settings
from vpipe_api.setup.machine import Machine
from vpipe_api.setup.pins import (
    MIN_MACOS_MAJOR,
    MODEL_DOWNLOAD_BYTES,
    VPIPE_BUILD_DISK_BYTES,
    VPIPE_CHECKOUT_BYTES,
)
from vpipe_api.setup.vpipe_build import binary_path
from vpipe_api.workflows.base import RequiredModel, WorkflowRegistry

# component id -> the workflow it makes servable
COMPONENTS = {"h3": "minimax-h3-turbo-video", "flashvsr": "flashvsr-upscale"}
DEFAULT_PREFIX = Path("~/.local/opt/vpipe-api")


class Status(StrEnum):
    INSTALLED = "installed"
    ALREADY = "already"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"


@dataclass(frozen=True)
class Fix:
    hint: str
    command: str | None = None
    needs_human: bool = False

    def to_json(self) -> dict[str, object]:
        return {"hint": self.hint, "command": self.command, "needsHuman": self.needs_human}


@dataclass(frozen=True)
class Report:
    id: str
    status: Status
    message: str
    download_bytes: int = 0
    disk_bytes: int = 0  # free space needed at the peak of the install
    fix: Fix | None = None

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "id": self.id,
            "status": str(self.status),
            "message": self.message,
            "downloadBytes": self.download_bytes,
        }
        if self.fix is not None:
            out["fix"] = self.fix.to_json()
        return out


@dataclass(frozen=True)
class Layout:
    """Where vpipe and its models are (configured) or go (under the prefix)."""

    vpipe_src: Path
    vpipe_bin: Path
    work_dir: Path

    @classmethod
    def resolve(cls, settings: Settings, prefix: Path) -> Layout:
        src = settings.resolved_vpipe_src_dir or prefix / "vpipe"
        return cls(
            vpipe_src=src,
            vpipe_bin=settings.vpipe_bin or binary_path(src),
            work_dir=settings.work_dir or prefix / "work",
        )

    @property
    def vpipe_built(self) -> bool:
        return self.vpipe_bin.is_file()

    def config_keys(self) -> dict[str, str]:
        return {
            "vpipe_bin": str(self.vpipe_bin),
            "vpipe_src_dir": str(self.vpipe_src),
            "work_dir": str(self.work_dir),
        }

    def settings(self, settings: Settings) -> Settings:
        return settings.model_copy(
            update={
                "vpipe_bin": self.vpipe_bin,
                "vpipe_src_dir": self.vpipe_src,
                "work_dir": self.work_dir,
            }
        )


def missing_models(
    component: str, layout: Layout, registry: WorkflowRegistry
) -> list[RequiredModel]:
    workflow = registry.get(COMPONENTS[component])
    if workflow is None:  # pragma: no cover - every component's workflow is built in
        raise KeyError(component)
    return list(workflow.missing_models(layout.work_dir))


def vpipe_cost(layout: Layout) -> tuple[int, int]:
    """Download and disk bytes still needed for vpipe itself (0, 0 once built)."""
    if layout.vpipe_built:
        return 0, 0
    clone = 0 if layout.vpipe_src.exists() else VPIPE_CHECKOUT_BYTES
    return clone, VPIPE_BUILD_DISK_BYTES


def _unsupported_fix(machine: Machine) -> Fix:
    if machine.vpipe_unsupported():
        return Fix(
            f"use an Apple Silicon Mac on macOS {MIN_MACOS_MAJOR} or later", needs_human=True
        )
    if not machine.tools.get("xcode"):
        return Fix(
            "install Xcode from the App Store, open it once, then rerun setup", needs_human=True
        )
    if not machine.tools.get("cmake"):
        return Fix("install cmake", command="brew install cmake")
    return Fix("install git", command="xcode-select --install", needs_human=True)


def _gb(n: int) -> str:
    return f"{n / 1e9:.0f} GB"


def inspect_component(
    component: str, layout: Layout, machine: Machine, registry: WorkflowRegistry
) -> Report:
    models = missing_models(component, layout, registry)
    if not models and layout.vpipe_built:
        return Report(component, Status.INSTALLED, f"vpipe at {layout.vpipe_bin}, models present")
    reason = machine.vpipe_unsupported() or (
        None if layout.vpipe_built else machine.build_unsupported()
    )
    if reason:
        return Report(component, Status.UNSUPPORTED, reason, fix=_unsupported_fix(machine))
    by_hand = [m.key for m in models if not m.can_setup]
    if by_hand:
        return Report(
            component,
            Status.UNSUPPORTED,
            f"{', '.join(by_hand)} cannot be fetched by setup",
            fix=Fix("prepare the model by hand in <work_dir>/models", needs_human=True),
        )
    build_download, build_disk = vpipe_cost(layout)
    download = build_download + sum(MODEL_DOWNLOAD_BYTES.get(m.key, 0) for m in models)
    disk = build_disk + sum(m.disk_gb_needed for m in models) * 10**9
    parts = ([] if layout.vpipe_built else ["build vpipe"]) + [m.key for m in models]
    return Report(
        component,
        Status.MISSING,
        f"to do: {', '.join(parts)} (~{_gb(download)} download, ~{_gb(disk)} free at the peak)",
        download_bytes=download,
        disk_bytes=disk,
    )
