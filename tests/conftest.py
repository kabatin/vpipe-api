"""Shared fixtures: a scriptable fake ``vpipe`` executable and settings pointing at it."""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from vpipe_api.settings import Settings, load_settings

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")

# Behaviour is selected with FAKE_VPIPE_MODE:
#   ok (default) — print progress, render the requested clip (with an audio track)
#   stage_fail   — log a stage failure but exit 0, like real vpipe
#   no_output    — succeed without writing the output
#   exit1        — exit with status 1
#   hang         — sleep until killed
#   grow         — (setup tests) append to $FAKE_GROW_FILE on each run, then succeed
FAKE_VPIPE = textwrap.dedent(
    """
    import json, os, signal, subprocess, sys, time
    args = sys.argv[1:]
    if args and args[0] == "--help":
        print("vpipe -- command-line entrance to libvpipe."); sys.exit(0)
    spec = json.load(open(args[args.index("--launch") + 1], encoding="utf-8"))
    mode = os.environ.get("FAKE_VPIPE_MODE", "ok")
    log = os.environ.get("FAKE_VPIPE_CALLS")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(spec, ensure_ascii=False) + "\\n")
    stages = {s["id"]: s for s in spec.get("stages", [])}
    print("[INFO] PipelineRuntime: pipeline launched", flush=True)
    if mode == "exit1":
        print("vpipe: launch failed", flush=True); sys.exit(1)
    if mode == "hang":
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        while True: time.sleep(0.1)
    if mode == "stage_fail":
        print("[WARN] stage 'generate-video' process: out of memory; entering drain", flush=True)
        sys.exit(0)
    if mode == "grow":
        with open(os.environ["FAKE_GROW_FILE"], "ab") as fh: fh.write(b"x" * 1024)
        sys.exit(0)
    for pct in (0, 50, 100):
        sys.stdout.write(f"[PROGRESS] {pct}% of 'denoise' completed at 00:00:00\\r")
        sys.stdout.flush()
    print("", flush=True)
    if mode == "no_output" or "save-video" not in stages:
        sys.exit(0)
    gen = stages["generate-video"]["config"]
    out = stages["save-video"]["config"]["output_url"]
    size = f"{gen['width']}x{gen['height']}"
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000",
                    "-frames:v", str(gen["frames"]), "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", out], check=True)
    print("[INFO] PipelineRuntime: pipeline 'x' ran for 1 s", flush=True)
    """
)


@pytest.fixture
def fake_vpipe(tmp_path: Path) -> Path:
    script = tmp_path / "bin" / "vpipe"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\n{FAKE_VPIPE}", encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def settings(tmp_path: Path, fake_vpipe: Path, work_dir: Path) -> Settings:
    return load_settings(
        env={
            "VPIPE_API_VPIPE_BIN": str(fake_vpipe),
            "VPIPE_API_WORK_DIR": str(work_dir),
            "VPIPE_API_DATA_DIR": str(tmp_path / "data"),
        },
        config_path=tmp_path / "absent.toml",
    )


@pytest.fixture
def fake_mode(monkeypatch: pytest.MonkeyPatch):
    def set_mode(mode: str) -> None:
        monkeypatch.setenv("FAKE_VPIPE_MODE", mode)

    return set_mode


def make_png(path: Path, size: tuple[int, int] = (64, 48), color: str = "red") -> bytes:
    from PIL import Image

    Image.new("RGB", size, color).save(path, format="PNG")
    return path.read_bytes()


def install_models(work_dir: Path) -> None:
    """Create the files ``minimax-h3-turbo-video`` expects, so doctor/setup see them."""
    from vpipe_api.workflows.h3_video import _DEFAULT_MODELS

    for model in _DEFAULT_MODELS:
        root = work_dir / "models" / model.path
        for name in model.files:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(b"x")


os.environ.setdefault("PYTHONUNBUFFERED", "1")
