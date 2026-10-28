"""Files setup writes: whole or not at all, in folders only the user can open."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from pathlib import Path


def make_private_dirs(path: Path) -> None:
    """Create ``path`` and any missing parent with mode 0700; existing ones are left alone."""
    missing = [p for p in (path, *path.parents) if not p.exists()]
    for folder in reversed(missing):
        folder.mkdir(mode=0o700)
        folder.chmod(0o700)  # the umask may have narrowed mkdir's mode, never widened it


def write_atomic(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    check: Callable[[Path], None] | None = None,
) -> None:
    """Write a temp file next to ``path``, let ``check`` inspect it, then rename it over
    ``path``. A symlink is written through: its target is replaced, the link stays."""
    target = path.resolve() if path.is_symlink() else path
    make_private_dirs(target.parent)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        tmp.chmod(mode)
        if check is not None:
            check(tmp)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
