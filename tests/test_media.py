import io
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from tests.conftest import make_png, needs_ffmpeg
from vpipe_api.media import (
    IMAGE_DECODERS,
    MediaError,
    finalize_video,
    normalize_image,
    probe_video,
)


def test_normalize_png(tmp_path: Path) -> None:
    out = normalize_image(make_png(tmp_path / "a.png"), "image/png")
    with Image.open(io.BytesIO(out)) as img:
        assert (img.format, img.mode, img.size) == ("PNG", "RGB", (64, 48))


def test_normalize_applies_exif_orientation() -> None:
    img = Image.new("RGB", (40, 20), "blue")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW on display
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    out = normalize_image(buf.getvalue(), "image/jpeg")
    with Image.open(io.BytesIO(out)) as upright:
        assert upright.size == (20, 40)


def _webp(img: Image.Image, **save_args: object) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="WEBP", **save_args)
    return buf.getvalue()


def _animated_webp() -> bytes:
    first, second = (Image.new("RGB", (64, 48), c) for c in ("red", "blue"))
    return _webp(first, save_all=True, append_images=[second], duration=100)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(_webp(Image.new("RGB", (64, 48), "red")), id="lossy"),
        pytest.param(_webp(Image.new("RGB", (64, 48), "red"), lossless=True), id="lossless"),
        pytest.param(_webp(Image.new("RGBA", (64, 48), (255, 0, 0, 128))), id="alpha"),
        pytest.param(_webp(Image.new("L", (64, 48), 128)), id="grayscale"),
        pytest.param(_animated_webp(), id="animated"),
    ],
)
def test_normalize_webp(data: bytes) -> None:
    out = normalize_image(data, "image/webp")
    with Image.open(io.BytesIO(out)) as img:
        assert (img.format, img.mode, img.size) == ("PNG", "RGB", (64, 48))


def test_normalize_webp_applies_exif_orientation() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW on display
    out = normalize_image(_webp(Image.new("RGB", (40, 20), "blue"), exif=exif), "image/webp")
    with Image.open(io.BytesIO(out)) as upright:
        assert upright.size == (20, 40)


def test_normalize_accepts_mpo_as_jpeg() -> None:
    first, second = Image.new("RGB", (40, 20), "red"), Image.new("RGB", (40, 20), "blue")
    buf = io.BytesIO()
    first.save(buf, format="MPO", save_all=True, append_images=[second])
    out = normalize_image(buf.getvalue(), "image/jpeg")
    with Image.open(io.BytesIO(out)) as img:
        assert (img.format, img.size) == ("PNG", (40, 20))


def test_every_image_decoder_has_a_pillow_opener() -> None:
    Image.init()
    assert set(IMAGE_DECODERS) <= set(Image.OPEN)


def test_normalize_rejects_mismatch_and_garbage(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="does not match"):
        normalize_image(make_png(tmp_path / "a.png"), "image/webp")
    with pytest.raises(MediaError, match="cannot decode"):
        normalize_image(b"not an image", "image/png")
    gif = io.BytesIO()
    Image.new("RGB", (4, 4)).save(gif, format="GIF")
    with pytest.raises(MediaError, match="cannot decode"):  # GIF decoder is never tried
        normalize_image(gif.getvalue(), "image/png")


def _clip(path: Path, size: str = "320x180", frames: int = 24, audio: bool = True) -> Path:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=sample_rate=32000", "-shortest", "-c:a", "aac"]
    cmd += ["-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


@needs_ffmpeg
def test_probe_video(tmp_path: Path) -> None:
    info = probe_video(_clip(tmp_path / "c.mp4"))
    assert (info.width, info.height, info.frames, info.fps, info.has_audio) == (
        320,
        180,
        24,
        24.0,
        True,
    )


@needs_ffmpeg
def test_finalize_scales_crops_and_drops_audio(tmp_path: Path) -> None:
    src = _clip(tmp_path / "c.mp4", size="832x480")
    dst = tmp_path / "out" / "o.mp4"
    finalize_video(
        src, dst, width=1280, height=720, comment="vpipe-job:abc", source_size=(832, 480)
    )
    info = probe_video(dst)
    assert (info.width, info.height, info.frames, info.has_audio) == (1280, 720, 24, False)
    tags = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format_tags=comment",
            "-of",
            "csv=p=0",
            str(dst),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tags == "vpipe-job:abc"
    assert not dst.with_suffix(".part.mp4").exists()


@needs_ffmpeg
def test_finalize_copies_when_size_matches(tmp_path: Path) -> None:
    src = _clip(tmp_path / "c.mp4")
    dst = tmp_path / "o.mp4"
    finalize_video(src, dst, width=320, height=180, comment="x", source_size=(320, 180))
    assert probe_video(dst).has_audio is False


def test_missing_binary_is_a_media_error(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="failed to run"):
        probe_video(tmp_path / "missing.mp4", ffprobe="definitely-not-ffprobe")


@needs_ffmpeg
def test_ffprobe_failure_is_a_media_error(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="exited"):
        probe_video(tmp_path / "missing.mp4")
