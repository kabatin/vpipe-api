"""vpipe runs this server did not start: an experiment, ``doctor --smoke``, a manual run.

They share the GPU and unified memory with the server's jobs, so the queue holds its next
job while one is going on, and ``/v1/health`` reports them as ``outside_runs``. Only the
``vpipe`` command line is seen (``--launch`` / ``--launch-stage``); apps that run pipelines
in their own process through libvpipe are not. A model download (``--launch-stage
model-fetch``, or the fetch step of ``vpipe-api setup models`` with its spec in
``<data_dir>/setup/<name>-fetch/``) uses only network and disk and is not counted.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

PS_TIMEOUT_S = 5
log = logging.getLogger("vpipe_api.outside")


def outside_runs() -> int | None:
    """How many vpipe runs not started by this process are going on; None if unknown."""
    try:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,ucomm=,args="],
            capture_output=True,
            text=True,
            errors="replace",  # any process's arguments may hold bytes that are not UTF-8
            timeout=PS_TIMEOUT_S,
            check=True,
        ).stdout
    except Exception:  # whatever ps does, it must not take the server down
        log.warning("cannot list processes; vpipe runs outside the server unknown", exc_info=True)
        return None
    return count_outside(listing, own_pid=os.getpid())


def count_outside(ps_listing: str, *, own_pid: int) -> int:
    """Count vpipe runs in ``ps -o pid=,ppid=,ucomm=,args=`` output whose parent is not
    ``own_pid`` (the server's own job), leaving out model downloads."""
    count = 0
    for line in ps_listing.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 4 or not fields[1].isdigit() or fields[2] != "vpipe":
            continue
        if int(fields[1]) != own_pid and _is_run(fields[3].split()):
            count += 1
    return count


def _is_run(argv: list[str]) -> bool:
    if "--launch-stage" in argv:
        return _after(argv, "--launch-stage") != "model-fetch"
    if "--launch" in argv:
        spec = Path(_after(argv, "--launch"))
        return not (spec.parent.name.endswith("-fetch") and spec.parent.parent.name == "setup")
    return False  # --help, a zombie's "<defunct>", ...


def _after(argv: list[str], flag: str) -> str:
    index = argv.index(flag) + 1
    return argv[index] if index < len(argv) else ""
