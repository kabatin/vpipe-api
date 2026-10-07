import threading
import time
from pathlib import Path

import pytest

from tests.conftest import needs_ffmpeg
from vpipe_api.runner import RunResult, VpipeRunner


def spec_for(out: Path, frames: int = 5) -> dict:
    return {
        "id": "t",
        "stages": [
            {
                "id": "generate-video",
                "type": "generate-video",
                "config": {"width": 64, "height": 64, "frames": frames},
            },
            {"id": "save-video", "type": "save-video", "config": {"output_url": str(out)}},
        ],
    }


def run(runner: VpipeRunner, tmp_path: Path, **kw) -> RunResult:
    out = tmp_path / "run" / "raw.mkv"
    return runner.run(
        spec_for(out),
        tmp_path / "run",
        expected_outputs=[out],
        timeout_s=kw.pop("timeout_s", 60),
        cancel=kw.pop("cancel", threading.Event()),
        **kw,
    )


@needs_ffmpeg
def test_ok_with_progress(fake_vpipe: Path, work_dir: Path, tmp_path: Path) -> None:
    seen: list[tuple[str, float]] = []
    result = run(
        VpipeRunner(fake_vpipe, work_dir),
        tmp_path,
        on_progress=lambda phase, f: seen.append((phase, f)),
    )
    assert result.ok, result.describe_failure()
    assert ("denoise", 1.0) in seen
    assert (tmp_path / "run" / "pipeline.vpipeline").is_file()
    assert "pipeline launched" in result.log_path.read_text()


def test_stage_failure_with_exit_zero(
    fake_vpipe: Path, work_dir: Path, tmp_path: Path, fake_mode
) -> None:
    fake_mode("stage_fail")
    result = run(VpipeRunner(fake_vpipe, work_dir), tmp_path)
    assert result.returncode == 0
    assert not result.ok
    assert "generate-video" in result.describe_failure()


@pytest.mark.parametrize(("mode", "needle"), [("no_output", "no output"), ("exit1", "code 1")])
def test_other_failures(
    fake_vpipe: Path, work_dir: Path, tmp_path: Path, fake_mode, mode: str, needle: str
) -> None:
    fake_mode(mode)
    result = run(VpipeRunner(fake_vpipe, work_dir), tmp_path)
    assert not result.ok
    assert needle in result.describe_failure()


def test_stale_output_does_not_count(
    fake_vpipe: Path, work_dir: Path, tmp_path: Path, fake_mode
) -> None:
    fake_mode("no_output")
    stale = tmp_path / "run" / "raw.mkv"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    result = run(VpipeRunner(fake_vpipe, work_dir), tmp_path)
    assert result.missing_outputs == (stale,)


def test_cancel_stops_process(fake_vpipe: Path, work_dir: Path, tmp_path: Path, fake_mode) -> None:
    fake_mode("hang")
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    started = time.monotonic()
    result = run(VpipeRunner(fake_vpipe, work_dir, stop_grace_s=5), tmp_path, cancel=cancel)
    assert result.canceled and not result.ok
    assert result.describe_failure() == "canceled"
    assert time.monotonic() - started < 10


def test_timeout_kills(fake_vpipe: Path, work_dir: Path, tmp_path: Path, fake_mode) -> None:
    fake_mode("hang")
    result = run(VpipeRunner(fake_vpipe, work_dir, stop_grace_s=5), tmp_path, timeout_s=1)
    assert result.timed_out
    assert "timed out" in result.describe_failure()


def test_describe_unknown() -> None:
    assert RunResult(returncode=0, duration_s=0, log_path=Path("x")).describe_failure() == (
        "unknown failure"
    )
