import json
import threading
from pathlib import Path

import pytest

from tests.conftest import install_models
from vpipe_api.runner import RunResult
from vpipe_api.settings import Settings, load_settings
from vpipe_api.setup.models import ModelPreparer, dir_size, fetch_only
from vpipe_api.setup.vpipe_build import KNOWN_COMMITS, SetupError, binary_path, build_vpipe
from vpipe_api.workflows.base import MediaTools
from vpipe_api.workflows.h3_video import H3VideoWorkflow

# -- build ---------------------------------------------------------------------------


def test_build_runs_clone_configure_build(tmp_path: Path) -> None:
    src = tmp_path / "vpipe"
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(list(cmd))
        if cmd[:2] == ["cmake", "--build"]:
            binary_path(src).parent.mkdir(parents=True)
            binary_path(src).write_text("")
        return 0

    messages: list[str] = []
    out = build_vpipe(
        src,
        run=run,
        emit=messages.append,
        has_tool=lambda _: True,
        metal_available=lambda: False,
        head_commit=lambda _: KNOWN_COMMITS["v0.1.80"],
    )
    assert out == binary_path(src)
    assert calls[0][:3] == ["git", "clone", "--recursive"] and "v0.1.80" in calls[0]
    assert "-DVPIPE_BUILD_PYTHON=OFF" in calls[1]
    assert any("Metal Toolchain" in m for m in messages)


def test_build_existing_checkout_and_errors(tmp_path: Path) -> None:
    src = tmp_path / "vpipe"
    src.mkdir()
    with pytest.raises(SetupError, match="cmake"):
        build_vpipe(src, run=lambda _: 0, has_tool=lambda name: name != "cmake")
    with pytest.raises(SetupError, match="configure"):
        build_vpipe(src, run=lambda _: 1, has_tool=lambda _: True, metal_available=lambda: True)
    with pytest.raises(SetupError, match="missing"):
        build_vpipe(src, run=lambda _: 0, has_tool=lambda _: True, metal_available=lambda: True)
    with pytest.raises(SetupError, match="clone"):
        build_vpipe(
            tmp_path / "new", run=lambda _: 1, has_tool=lambda _: True, metal_available=lambda: True
        )


# -- models --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setup tests must not depend on the free space of the machine running them."""
    import shutil

    from vpipe_api.setup import models as models_mod

    monkeypatch.setattr(
        models_mod.shutil,
        "disk_usage",
        lambda _: shutil._ntuple_diskusage(10**13, 0, 10**13),  # type: ignore[attr-defined]
    )


def test_fetch_only_keeps_model_fetch_stages() -> None:
    spec = {
        "id": "p",
        "stages": [
            {"id": "f", "type": "model-fetch", "iports": [], "config": {"a": 1}},
            {"id": "q", "type": "model-quantize", "iports": [{"src": "f"}], "config": {}},
        ],
    }
    assert fetch_only(spec) == {
        "id": "p-fetch",
        "stages": [{"id": "f", "type": "model-fetch", "config": {"a": 1}}],
    }


def test_dir_size(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x").write_bytes(b"12345")
    assert dir_size(tmp_path) == 5


@pytest.fixture
def src_settings(tmp_path: Path, settings: Settings) -> Settings:
    pipelines = tmp_path / "src" / "docs" / "pipelines"
    pipelines.mkdir(parents=True)
    for name in ("prepare-minimax-h3-8bit", "prepare-minimax-h3-turbo-lora"):
        (pipelines / f"{name}.vpipeline").write_text(
            json.dumps(
                {
                    "id": name,
                    "stages": [
                        {"id": "fetch", "type": "model-fetch", "iports": [], "config": {}},
                    ],
                }
            )
        )
    return settings.model_copy(update={"vpipe_src_dir": tmp_path / "src"})


class ScriptedRunner:
    """Each ``run`` pops a behaviour: 'stall' (wait for cancel), 'ok', 'install', 'fail'."""

    def __init__(self, work_dir: Path, script: list[str]) -> None:
        self.work_dir = work_dir
        self.script = script
        self.calls: list[str] = []

    def run(self, spec, run_dir, *, expected_outputs, timeout_s, cancel, on_progress=None):
        action = self.script.pop(0)
        self.calls.append(f"{spec['id']}:{action}")
        log = run_dir / "vpipe.log"
        if action == "stall":
            cancel.wait(5)
            return RunResult(returncode=0, duration_s=1, log_path=log, canceled=True)
        if action == "install":
            install_models(self.work_dir)
        if action == "fail":
            return RunResult(returncode=0, duration_s=1, log_path=log, failures=("boom",))
        return RunResult(returncode=0, duration_s=1, log_path=log)


def test_prepare_restarts_a_stalled_download(src_settings: Settings, work_dir: Path) -> None:
    runner = ScriptedRunner(work_dir, ["stall", "ok", "install", "ok", "ok"])
    messages: list[str] = []
    preparer = ModelPreparer(
        src_settings,
        runner=runner,
        emit=messages.append,  # type: ignore[arg-type]
        stall_s=0.2,
        poll_s=0.1,
    )
    prepared = preparer.prepare(H3VideoWorkflow(MediaTools()))
    assert prepared == ["local/MiniMax-H3-FL2VA-8bit"]
    assert runner.calls[:3] == [
        "prepare-minimax-h3-8bit-fetch:stall",
        "prepare-minimax-h3-8bit-fetch:ok",
        "prepare-minimax-h3-8bit:install",
    ]
    assert any("stalled" in m for m in messages)
    assert any("already present" in m for m in messages)


def test_prepare_errors(src_settings: Settings, work_dir: Path) -> None:
    fail = ScriptedRunner(work_dir, ["ok", "fail"])
    with pytest.raises(SetupError, match="failed"):
        ModelPreparer(src_settings, runner=fail).prepare(  # type: ignore[arg-type]
            H3VideoWorkflow(MediaTools())
        )
    gives_up = ScriptedRunner(work_dir, ["stall", "stall"])
    with pytest.raises(SetupError, match="did not finish"):
        ModelPreparer(
            src_settings,
            runner=gives_up,
            stall_s=0.2,
            poll_s=0.1,  # type: ignore[arg-type]
            max_restarts=1,
        ).prepare(H3VideoWorkflow(MediaTools()))
    still_missing = ScriptedRunner(work_dir, ["ok", "ok"])
    with pytest.raises(SetupError, match="still missing"):
        ModelPreparer(src_settings, runner=still_missing).prepare(  # type: ignore[arg-type]
            H3VideoWorkflow(MediaTools())
        )


def test_prepare_needs_pipelines_and_known_models(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(SetupError, match="docs/pipelines"):
        ModelPreparer(settings.model_copy(update={"vpipe_src_dir": tmp_path / "nope"}))
    src = tmp_path / "s" / "docs" / "pipelines"
    src.mkdir(parents=True)
    s = settings.model_copy(update={"vpipe_src_dir": tmp_path / "s"})
    assert settings.work_dir is not None
    runner = ScriptedRunner(settings.work_dir, [])
    with pytest.raises(SetupError, match="not found"):
        ModelPreparer(s, runner=runner).prepare(H3VideoWorkflow(MediaTools()))  # type: ignore[arg-type]
    install_models(settings.work_dir)  # the default LoRA is then present
    custom = H3VideoWorkflow(MediaTools(), {"model_key": "local/Custom"})
    with pytest.raises(SetupError, match="no prepare pipeline"):
        ModelPreparer(s, runner=runner).prepare(custom)  # type: ignore[arg-type]


def test_disk_space_guard(
    src_settings: Settings, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from vpipe_api.setup import models as models_mod

    monkeypatch.setattr(
        models_mod.shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(1, 1, 10**9)
    )  # type: ignore[attr-defined]
    with pytest.raises(SetupError, match="GB free"):
        ModelPreparer(src_settings, runner=ScriptedRunner(work_dir, [])).prepare(  # type: ignore[arg-type]
            H3VideoWorkflow(MediaTools())
        )


def test_resolved_src_dir(tmp_path: Path) -> None:
    s = load_settings(
        env={"VPIPE_API_VPIPE_BIN": "/x/vpipe/build/apps/vpipe/vpipe"}, config_path=tmp_path / "n"
    )
    assert s.resolved_vpipe_src_dir == Path("/x/vpipe")
    assert threading.active_count() >= 1
