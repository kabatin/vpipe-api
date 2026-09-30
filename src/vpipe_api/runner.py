"""Run one vpipe pipeline through the ``vpipe`` CLI and decide whether it really worked.

vpipe exits 0 even when a stage fails at runtime (the failure is only logged as
``stage '<id>' process: ...; entering drain``), so success is decided here from the log
*and* from the expected output files having been freshly written.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FAILURE_PATTERN = re.compile(r"stage '([^']+)' (initialize|process):(.*)")
PROGRESS_PATTERN = re.compile(r"\[PROGRESS\] (\d+)% of '([^']+)'")

ProgressCallback = Callable[[str, float], None]


@dataclass(frozen=True)
class RunResult:
    returncode: int | None
    duration_s: float
    log_path: Path
    failures: tuple[str, ...] = ()
    missing_outputs: tuple[Path, ...] = ()
    canceled: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return (
            self.returncode == 0
            and not self.failures
            and not self.missing_outputs
            and not self.canceled
            and not self.timed_out
        )

    def describe_failure(self) -> str:
        if self.canceled:
            return "canceled"
        if self.timed_out:
            return f"timed out after {self.duration_s:.0f}s"
        if self.failures:
            return "; ".join(self.failures[:3])
        if self.returncode not in (0, None):
            return f"vpipe exited with code {self.returncode}"
        if self.missing_outputs:
            return "no output written: " + ", ".join(p.name for p in self.missing_outputs)
        return "unknown failure"


@dataclass
class _Watch:
    failures: list[str] = field(default_factory=list)


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return -1


class VpipeRunner:
    """Launches ``vpipe --launch <spec>`` in the work directory (model registry lives there)."""

    def __init__(self, vpipe_bin: Path, work_dir: Path, *, stop_grace_s: float = 30.0) -> None:
        self._vpipe_bin = vpipe_bin
        self._work_dir = work_dir
        self._stop_grace_s = stop_grace_s

    def run(
        self,
        spec: dict[str, Any],
        run_dir: Path,
        *,
        expected_outputs: Sequence[Path],
        timeout_s: float,
        cancel: threading.Event,
        on_progress: ProgressCallback | None = None,
        extra_args: Sequence[str] = (),
    ) -> RunResult:
        run_dir.mkdir(parents=True, exist_ok=True)
        spec_path = run_dir / "pipeline.vpipeline"
        # vpipe's JSON parser does not join \u surrogate pairs: keep UTF-8 as-is.
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        log_path = run_dir / "vpipe.log"
        before = {path: _mtime_ns(path) for path in expected_outputs}

        started = time.monotonic()
        # line-buffered, so `tail -f vpipe.log` shows a running job live
        with log_path.open("w", encoding="utf-8", buffering=1) as log:
            proc = subprocess.Popen(
                [str(self._vpipe_bin), "--launch", str(spec_path), *extra_args],
                cwd=self._work_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,  # universal newlines: progress bars end in '\r'
                encoding="utf-8",
                errors="replace",
                start_new_session=True,  # own process group, so a stop reaches children too
            )
            watch = _Watch()
            reader = threading.Thread(
                target=self._pump, args=(proc, log, watch, on_progress), daemon=True
            )
            reader.start()
            canceled, timed_out = self._wait(proc, started, timeout_s, cancel)
            reader.join(timeout=10)

        missing = tuple(
            path
            for path in expected_outputs
            if not path.is_file() or path.stat().st_size == 0 or _mtime_ns(path) <= before[path]
        )
        return RunResult(
            returncode=proc.returncode,
            duration_s=time.monotonic() - started,
            log_path=log_path,
            failures=tuple(watch.failures),
            missing_outputs=missing,
            canceled=canceled,
            timed_out=timed_out,
        )

    @staticmethod
    def _pump(
        proc: subprocess.Popen[str],
        log: Any,
        watch: _Watch,
        on_progress: ProgressCallback | None,
    ) -> None:
        stream = proc.stdout
        if stream is None:
            return
        for line in stream:
            log.write(line)
            failure = FAILURE_PATTERN.search(line)
            if failure is not None:
                watch.failures.append(
                    f"stage '{failure.group(1)}' {failure.group(2)}:{failure.group(3)}".strip()
                )
            progress = PROGRESS_PATTERN.search(line)
            if progress is not None and on_progress is not None:
                on_progress(progress.group(2), min(int(progress.group(1)), 100) / 100.0)
        log.flush()

    def _wait(
        self,
        proc: subprocess.Popen[str],
        started: float,
        timeout_s: float,
        cancel: threading.Event,
    ) -> tuple[bool, bool]:
        while True:
            try:
                proc.wait(timeout=0.5)
                return False, False
            except subprocess.TimeoutExpired:
                pass
            if cancel.is_set():
                self._stop(proc)
                return True, False
            if time.monotonic() - started > timeout_s:
                self._stop(proc)
                return False, True

    def _stop(self, proc: subprocess.Popen[str]) -> None:
        """Ctrl-C first (vpipe drains cleanly), then SIGKILL the whole group."""
        for sig in (signal.SIGINT, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=self._stop_grace_s)
                return
            except subprocess.TimeoutExpired:
                continue
