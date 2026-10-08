import subprocess
import time

import pytest

from vpipe_api import outside
from vpipe_api.outside import count_outside, outside_runs

VPIPE = "/Users/me/vpipe/build/apps/vpipe/vpipe"
DATA = "/Users/me/.local/share/vpipe-api"


def test_counts_only_vpipe_runs_this_server_did_not_start() -> None:
    ps = "\n".join(
        [
            f"  101   100 vpipe    {VPIPE} --launch {DATA}/jobs/job_x/pipeline.vpipeline",  # ours
            f"  202     1 vpipe    {VPIPE} --launch /tmp/experiment/exp.vpipeline",  # counts
            f"  203   555 vpipe    {VPIPE} --launch {DATA}/smoke/abc/pipeline.vpipeline",  # smoke
            f"  204   556 vpipe    {VPIPE} --help",  # doctor's version check, not a run
            f"  205   557 vpipe    {VPIPE} --launch {DATA}/setup/x-fetch/pipeline.vpipeline",
            f"  206   558 vpipe    {VPIPE} --launch {DATA}/setup/prepare-x/pipeline.vpipeline",
            f"  207   559 vpipe    {VPIPE} --launch-stage generate-video",  # one stage: counts
            f"  208   560 vpipe    {VPIPE} --launch-stage model-fetch",  # a download
            "  209   561 vpipe    /Users/me/My Tools/vpipe --launch /tmp/x.vpipeline",  # spaces
            "  210   562 vpipe    <defunct>",  # a zombie is not running anything
            "  300     1 vim      /usr/bin/vim notes-about-vpipe --launch",
            "  301     1 zsh      /bin/zsh -c vpipe-api serve",
            "garbage line",
        ]
    )
    # counted: the experiment, the smoke run, the quantize step, the stage, the spaced path
    assert count_outside(ps, own_pid=100) == 5


def test_nothing_running() -> None:
    assert count_outside("", own_pid=100) == 0


def test_a_process_with_undecodable_arguments_does_not_break_the_count() -> None:
    odd = subprocess.Popen([b"/bin/sh", b"-c", b"sleep 5", b"x\xff\xfe"])
    try:
        time.sleep(0.2)
        assert isinstance(outside_runs(), int)
    finally:
        odd.kill()
        odd.wait()


@pytest.mark.parametrize(
    "error",
    [
        OSError("no ps"),
        subprocess.TimeoutExpired("ps", 5),
        subprocess.CalledProcessError(1, "ps"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
    ],
)
def test_unknown_when_ps_fails(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fail(*_: object, **__: object) -> None:
        raise error

    monkeypatch.setattr(outside.subprocess, "run", fail)
    assert outside_runs() is None
