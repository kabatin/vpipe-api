"""``vpipe-api setup vpipe``: clone and build vpipe (Apple Silicon, macOS 26+)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

DEFAULT_REPO = "https://github.com/tgo-app-dev/vpipe.git"
DEFAULT_TAG = "v0.1.80"
# Tags can be moved; the tags we test against are pinned to their commit.
KNOWN_COMMITS = {"v0.1.80": "a90cf05abb92863213adf7259a5f936266c92af2"}

Run = Callable[[Sequence[str]], int]
Emit = Callable[[str], None]


class SetupError(RuntimeError):
    """A setup step cannot continue; the message tells the user what to do."""


def _default_run(cmd: Sequence[str]) -> int:
    return subprocess.run(list(cmd), check=False).returncode


def _quiet_ok(cmd: Sequence[str]) -> bool:
    try:
        return subprocess.run(list(cmd), capture_output=True, check=False).returncode == 0
    except OSError:
        return False


def _git_head(src_dir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(src_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return result.stdout.strip() or "unknown"


def binary_path(src_dir: Path) -> Path:
    return src_dir / "build" / "apps" / "vpipe" / "vpipe"


def build_vpipe(
    src_dir: Path,
    *,
    tag: str = DEFAULT_TAG,
    repo: str = DEFAULT_REPO,
    run: Run = _default_run,
    emit: Emit = lambda _: None,
    has_tool: Callable[[str], bool] = lambda name: shutil.which(name) is not None,
    head_commit: Callable[[Path], str] = _git_head,
    metal_available: Callable[[], bool] = lambda: _quiet_ok(
        ["xcrun", "-sdk", "macosx", "metal", "--version"]
    ),
) -> Path:
    for tool, hint in (
        ("git", "install Xcode command line tools"),
        ("cmake", "brew install cmake"),
    ):
        if not has_tool(tool):
            raise SetupError(f"{tool} not found — {hint}")
    if not metal_available():
        emit(
            "warning: Metal Toolchain missing; vpipe will compile kernels at runtime. "
            "For faster first runs: xcodebuild -downloadComponent MetalToolchain"
        )

    if src_dir.exists():
        emit(f"using existing checkout {src_dir} (not changing its version)")
    else:
        emit(f"cloning vpipe {tag} into {src_dir}")
        if run(["git", "clone", "--recursive", "--branch", tag, repo, str(src_dir)]) != 0:
            raise SetupError("git clone failed")
        expected = KNOWN_COMMITS.get(tag)
        actual = head_commit(src_dir)
        if expected is None:
            emit(f"warning: {tag} is not a tested tag; building commit {actual}")
        elif actual != expected:
            raise SetupError(
                f"{tag} resolves to {actual}, expected {expected} — the tag was moved; "
                "check the repository before building"
            )

    build_dir = src_dir / "build"
    emit("configuring (Release, Python bindings off)")
    if run(["cmake", "-S", str(src_dir), "-B", str(build_dir), "-DVPIPE_BUILD_PYTHON=OFF"]) != 0:
        raise SetupError("cmake configure failed")
    emit("building (this takes ~20 minutes the first time)")
    if run(["cmake", "--build", str(build_dir), "-j"]) != 0:
        raise SetupError("cmake build failed")

    binary = binary_path(src_dir)
    if not binary.is_file():
        raise SetupError(f"build finished but {binary} is missing")
    return binary
