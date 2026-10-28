"""Facts about this Mac that decide what ``setup`` can install (read only, fast)."""

from __future__ import annotations

import platform
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from vpipe_api.setup.pins import MIN_MACOS_MAJOR

TOOLS = ("git", "cmake", "uv", "ffmpeg", "ffprobe")
BUILD_TOOLS = ("xcode", "cmake", "git")
Probe = Callable[[Sequence[str]], str | None]


@dataclass(frozen=True)
class Machine:
    arm64: bool
    macos: str  # "" off macOS
    ram_gb: int
    free_bytes: int
    tools: dict[str, bool] = field(default_factory=dict)

    @property
    def macos_major(self) -> int:
        head = self.macos.split(".", 1)[0]
        return int(head) if head.isdigit() else 0

    def vpipe_unsupported(self) -> str | None:
        """Why vpipe cannot run here, or None."""
        if not self.arm64:
            return "vpipe needs an Apple Silicon Mac"
        if self.macos_major < MIN_MACOS_MAJOR:
            return f"vpipe needs macOS {MIN_MACOS_MAJOR}+ (this Mac: macOS {self.macos or '?'})"
        return None

    def build_unsupported(self) -> str | None:
        """Why vpipe cannot be built here (on top of running it), or None."""
        missing = [name for name in BUILD_TOOLS if not self.tools.get(name)]
        if missing:
            return f"building vpipe needs {', '.join(missing)}"
        return None

    def to_json(self) -> dict[str, object]:
        return {
            "arm64": self.arm64,
            "macos": self.macos,
            "ramGb": self.ram_gb,
            "freeBytes": self.free_bytes,
            "tools": dict(self.tools),
        }


def _probe(cmd: Sequence[str]) -> str | None:
    try:
        result = subprocess.run(list(cmd), capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def existing_parent(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path("/")


def inspect_machine(
    disk_path: Path,
    *,
    probe: Probe = _probe,
    has_tool: Callable[[str], bool] = lambda name: shutil.which(name) is not None,
) -> Machine:
    memsize = probe(["sysctl", "-n", "hw.memsize"]) or ""
    tools = {name: has_tool(name) for name in TOOLS}
    # xcodebuild answers -version only with a full Xcode, not the command line tools alone
    tools["xcode"] = probe(["xcodebuild", "-version"]) is not None
    return Machine(
        # hw.optional.arm64: the chip, even when this Python runs under Rosetta
        arm64=platform.system() == "Darwin"
        and (
            platform.machine() == "arm64"
            or (probe(["sysctl", "-n", "hw.optional.arm64"]) or "").strip() == "1"
        ),
        macos=platform.mac_ver()[0],
        ram_gb=round(int(memsize) / 2**30) if memsize.strip().isdigit() else 0,
        free_bytes=shutil.disk_usage(existing_parent(disk_path)).free,
        tools=tools,
    )
