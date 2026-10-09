import io
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from tests.conftest import make_png, needs_ffmpeg
from vpipe_api.media import (
    IMAGE_DECODERS,
    MediaError,
    finalize_cropped,
    finalize_video,
    normalize_image,
    probe_video,
)

Finalize = Callable[..., None]


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


def _lossless_clip(
    path: Path,
    source: str = "testsrc=size=320x192",
    *,
    audio: bool = False,
    tagged: bool = True,
    rate: int = 24,
    paint: str = "",
) -> Path:
    """A clip shaped like vpipe's intermediate: FFV1, 4:4:4, full-range BT.709 samples.

    ``tagged=False`` keeps the full-range samples but drops the colour tags, as a remux can.
    ``paint`` is a filter that writes those samples directly (``geq`` on 4:4:4).
    """
    tags = ("range=pc:colorspace=bt709:color_primaries=bt709:color_trc=bt709", "-color_range", "pc")
    if not tagged:
        tags = ("range=unknown:colorspace=unknown:color_primaries=unknown:color_trc=unknown",)
    painted = f"{paint}," if paint else ""
    vf = f"scale=out_range=pc:out_color_matrix=bt709,format=yuv444p,{painted}setparams={tags[0]}"
    # -color_range pc, or format negotiation converts tagged full-range samples to tv
    codec = ["-c:v", "ffv1", "-pix_fmt", "yuv444p", *tags[1:]]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{source}:rate={rate}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=sample_rate=32000", "-shortest", "-c:a", "aac"]
    subprocess.run([*cmd, "-vf", vf, "-frames:v", "24", *codec, str(path)], check=True)
    return path


def _stream_fields(path: Path, fields: str) -> dict[str, str]:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", f"stream={fields}"]
    out = subprocess.run(
        [*cmd, "-of", "default=nw=1", str(path)], capture_output=True, text=True, check=True
    ).stdout
    return dict(line.split("=", 1) for line in out.split())


def _mean_luma(path: Path) -> float:
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", "extractplanes=y"]
    raw = subprocess.run(
        [*cmd, "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True
    ).stdout
    return sum(raw) / len(raw)


@needs_ffmpeg
def test_probe_counts_frames_in_matroska_with_longer_audio(tmp_path: Path) -> None:
    info = probe_video(_lossless_clip(tmp_path / "raw.mkv", audio=True))
    assert info.duration_sec * info.fps > 24.5  # duration x fps would say 25
    assert (info.frames, info.has_audio) == (24, True)


@needs_ffmpeg
def test_finalize_scales_crops_and_drops_audio(tmp_path: Path) -> None:
    src = _lossless_clip(tmp_path / "raw.mkv", "testsrc=size=832x480", audio=True)
    dst = tmp_path / "out" / "o.mp4"
    finalize_video(src, dst, width=1280, height=720, fps=24, comment="vpipe-job:abc")
    info = probe_video(dst)
    assert (info.width, info.height, info.frames, info.has_audio) == (1280, 720, 24, False)
    probe = ["ffprobe", "-v", "error", "-show_entries", "format_tags=comment", "-of", "csv=p=0"]
    tags = subprocess.run(
        [*probe, str(dst)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tags == "vpipe-job:abc"
    assert not dst.with_suffix(".part.mp4").exists()


FINALIZERS = pytest.mark.parametrize("finalize", [finalize_video, finalize_cropped])


@needs_ffmpeg
@FINALIZERS
def test_finalize_stamps_the_given_frame_rate(tmp_path: Path, finalize: Finalize) -> None:
    # vpipe's Matroska has millisecond timestamps and no frame rate; with large FFV1 frames
    # ffmpeg guesses the rate from a couple of them (24000/1001, 293/12). Here they say 25.
    src = _lossless_clip(tmp_path / "raw.mkv", rate=25)
    dst = tmp_path / "o.mp4"
    finalize(src, dst, width=320, height=192, fps=24, comment="x")
    fields = _stream_fields(dst, "r_frame_rate,avg_frame_rate,nb_frames,duration")
    assert fields == {
        "r_frame_rate": "24/1",
        "avg_frame_rate": "24/1",
        "nb_frames": "24",
        "duration": "1.000000",
    }


@needs_ffmpeg
def test_finalize_reencodes_a_lossless_source_of_the_same_size(tmp_path: Path) -> None:
    src = _lossless_clip(tmp_path / "raw.mkv", "color=gray:s=320x192")
    dst = tmp_path / "o.mp4"
    finalize_video(src, dst, width=320, height=192, fps=24, comment="x")
    fields = _stream_fields(dst, "codec_name,pix_fmt,width,height")
    assert fields == {"codec_name": "h264", "pix_fmt": "yuv420p", "width": "320", "height": "192"}


@needs_ffmpeg
@pytest.mark.parametrize(
    ("finalize", "size"),
    [(finalize_video, (640, 360)), (finalize_cropped, (320, 180))],  # a real lanczos upscale
)
@pytest.mark.parametrize(
    ("color", "tagged", "full"),
    [
        ("white", True, 255),
        ("black", True, 0),
        ("lime", True, 182),  # BT.709 luma of pure green; a BT.601 output would land near 145
        ("0x404040", False, 64),  # not tagged pc: still scaled as full range, not left at 64
    ],
)
def test_finalize_outputs_limited_range_bt709(
    tmp_path: Path, finalize: Finalize, size: tuple[int, int], color: str, tagged: bool, full: int
) -> None:
    src = _lossless_clip(tmp_path / "raw.mkv", f"color={color}:s=320x192", tagged=tagged)
    assert (_stream_fields(src, "color_range")["color_range"] == "pc") is tagged
    source_luma = _mean_luma(src)
    assert abs(source_luma - full) <= 3  # full-range samples (older ffmpeg rounds a little)
    dst = tmp_path / "o.mp4"
    finalize(src, dst, width=size[0], height=size[1], fps=24, comment="x")
    assert _stream_fields(dst, "color_range,color_space,color_primaries,color_transfer") == {
        "color_range": "tv",
        "color_space": "bt709",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
    }
    assert abs(_mean_luma(dst) - (16 + 219 * source_luma / 255)) <= 1.5


def _luma_lines(path: Path, width: int, height: int, axis: str) -> list[float]:
    """Mean luma of each row (``axis`` "Y") or column ("X") of the first frame."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", "extractplanes=y"]
    raw = subprocess.run(
        [*cmd, "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True
    ).stdout
    assert len(raw) == width * height
    if axis == "Y":
        return [sum(raw[y * width : (y + 1) * width]) / width for y in range(height)]
    return [sum(raw[x::width]) / height for x in range(width)]


@needs_ffmpeg
@pytest.mark.parametrize(("axis", "size"), [("Y", (768, 754)), ("X", (754, 768))])
def test_finalize_cropped_keeps_the_centre_at_the_source_size(
    tmp_path: Path, axis: str, size: tuple[int, int]
) -> None:
    # 768 lines to 754 drops 7 on each side: an odd offset that 4:2:0 cannot crop at
    bands = f"if(lt({axis},7)+gte({axis},761),0,if(eq({axis},7)+eq({axis},760),255,128))"
    paint = f"geq=lum='{bands}':cb=128:cr=128"
    src = _lossless_clip(tmp_path / "raw.mkv", "color=gray:s=768x768", paint=paint)
    assert [round(v) for v in _luma_lines(src, 768, 768, axis)[5:9]] == [0, 0, 255, 128]
    dst = tmp_path / "out" / "o.mp4"
    finalize_cropped(src, dst, width=size[0], height=size[1], fps=24, comment="vpipe-job:abc")
    info = probe_video(dst)
    assert (info.width, info.height, info.frames, info.has_audio) == (*size, 24, False)
    lines = _luma_lines(dst, *size, axis)
    white, grey = 16 + 219, 16 + 219 * 128 / 255
    assert abs(lines[0] - white) < 12 and abs(lines[-1] - white) < 12  # source lines 7, 760
    assert all(abs(v - grey) < 12 for v in lines[2:-2])
    assert _stream_fields(dst, "pix_fmt,sample_aspect_ratio") == {
        "pix_fmt": "yuv420p",
        "sample_aspect_ratio": "1:1",
    }


@needs_ffmpeg
def test_finalize_cropped_cannot_grow_the_clip(tmp_path: Path) -> None:
    src = _lossless_clip(tmp_path / "raw.mkv")
    with pytest.raises(MediaError, match="exited"):
        finalize_cropped(src, tmp_path / "o.mp4", width=640, height=360, fps=24, comment="x")


def test_missing_binary_is_a_media_error(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="failed to run"):
        probe_video(tmp_path / "missing.mp4", ffprobe="definitely-not-ffprobe")


@needs_ffmpeg
def test_ffprobe_failure_is_a_media_error(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="exited"):
        probe_video(tmp_path / "missing.mp4")
