"""ffmpeg helpers for ``flashvsr-upscale``: source checks, crop + pad, and the final encode."""

import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from tests.conftest import needs_ffmpeg
from vpipe_api.media import (
    MediaError,
    finalize_upscale,
    pad_and_crop_source,
    probe_source,
    probe_video,
)

pytestmark = needs_ffmpeg


def _mp4(
    path: Path,
    *,
    size: str = "320x180",
    frames: int = 24,
    rate: str = "24",
    audio: bool = False,
    vcodec: str = "libx264",
) -> Path:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=sample_rate=48000", "-c:a", "aac", "-shortest"]
    cmd += ["-frames:v", str(frames), "-c:v", vcodec, "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


def _fields(path: Path, entries: str) -> dict[str, str]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v",
            "-show_entries",
            entries,
            "-of",
            "default=nw=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(line.split("=", 1) for line in out.split())


def _with_subtitles(path: Path) -> None:
    srt = path.with_suffix(".srt")
    srt.write_text("1\n00:00:00,000 --> 00:00:00,500\nhi\n")
    video = _mp4(path.with_name("v.mp4"))
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(video),
            "-i",
            str(srt),
            "-c:v",
            "copy",
            "-c:s",
            "mov_text",
            str(path),
        ],
        check=True,
    )


def test_probe_source_reads_an_h3_style_clip(tmp_path: Path) -> None:
    info = probe_source(_mp4(tmp_path / "s.mp4", frames=56))
    assert (info.width, info.height, info.frames, info.rate, info.audio_codec) == (
        320,
        180,
        56,
        Fraction(24),
        None,
    )


def test_probe_source_keeps_an_ntsc_rate_and_sees_audio(tmp_path: Path) -> None:
    info = probe_source(_mp4(tmp_path / "s.mp4", rate="24000/1001", audio=True))
    assert (info.rate, info.audio_codec) == (Fraction(24000, 1001), "aac")


def test_probe_source_counts_the_frames_an_edit_list_keeps(tmp_path: Path) -> None:
    gop = _mp4(tmp_path / "gop.mp4", frames=120)
    trimmed = tmp_path / "trimmed.mp4"  # cut at a non-keyframe, as editors and -c copy do
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(gop), "-c", "copy", str(trimmed)],
        check=True,
    )
    decoded = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "csv=p=0",
            str(trimmed),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert int(decoded) < 120  # the header still says 120
    assert probe_source(trimmed).frames == int(decoded)


def test_probe_source_reports_the_upright_size(tmp_path: Path) -> None:
    rotated = tmp_path / "phone.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-display_rotation:v",
            "90",
            "-i",
            str(_mp4(tmp_path / "s.mp4")),
            "-c",
            "copy",
            str(rotated),
        ],
        check=True,
    )
    info = probe_source(rotated)
    assert (info.width, info.height) == (180, 320)
    padded = tmp_path / "padded.mkv"
    pad_and_crop_source(rotated, padded, crop=(180, 320), size=None, front=0, back=0)
    assert _fields(padded, "stream=width,height") == {"width": "180", "height": "320"}


def test_probe_source_refuses_hdr(tmp_path: Path) -> None:
    hlg = tmp_path / "hlg.mp4"
    tags = "setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc"
    subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24"),
            *("-vf", tags, "-frames:v", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(hlg)),
        ],
        check=True,
    )
    with pytest.raises(MediaError, match="HDR"):
        probe_source(hlg)


def test_probe_source_refuses_more_than_8_bits(tmp_path: Path) -> None:
    deep = tmp_path / "deep.mp4"
    made = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=24",
            "-frames:v",
            "5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p10le",
            str(deep),
        ],
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("this ffmpeg's libx264 has no 10-bit")
    with pytest.raises(MediaError, match="8-bit"):
        probe_source(deep)


def test_encode_timeout_grows_with_the_work() -> None:
    from vpipe_api.media import encode_timeout

    assert encode_timeout(56, 1920, 1080) == 600  # a typical take: the floor
    assert encode_timeout(2400, 4096, 2304) > 3000  # 40 s at 60 fps, 4K: far past 600 s


def test_probe_source_refuses_more_than_60_fps(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="frame rate"):
        probe_source(_mp4(tmp_path / "s.mp4", rate="120"))


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda p: p.write_bytes(b"not a video at all"), "not a readable MP4"),
        (lambda p: _mp4(p.with_suffix(".mkv")).replace(p), "not a readable MP4"),
        (lambda p: _mp4(p, vcodec="mpeg4"), "only H.264/HEVC video and AAC audio"),
        (_with_subtitles, "only H.264/HEVC video and AAC audio"),
    ],
    ids=["garbage", "matroska", "mpeg4-part2", "subtitles"],
)
def test_probe_source_refuses_what_it_does_not_trust(tmp_path: Path, make, message: str) -> None:
    path = tmp_path / "s.mp4"
    make(path)
    with pytest.raises(MediaError, match=message):
        probe_source(path)


def test_pad_and_crop_scales_down_to_the_processing_size(tmp_path: Path) -> None:
    dst = tmp_path / "padded.mkv"
    src = _mp4(tmp_path / "s.mp4", size="640x360", frames=3)
    pad_and_crop_source(src, dst, crop=(640, 360), size=(256, 128), front=0, back=0)
    assert _fields(dst, "stream=width,height") == {"width": "256", "height": "128"}


def test_pad_and_crop_clones_edge_frames_losslessly(tmp_path: Path) -> None:
    src = _mp4(tmp_path / "s.mp4", size="320x192", frames=10)
    dst = tmp_path / "padded.mkv"
    pad_and_crop_source(src, dst, crop=(320, 180), size=None, front=4, back=7)
    fields = _fields(dst, "stream=codec_name,width,height")
    assert fields == {"codec_name": "ffv1", "width": "320", "height": "180"}
    assert probe_video(dst).frames == 4 + 10 + 7
    md5 = _frame_md5(dst)
    assert md5[:5] == [md5[4]] * 5  # four clones of the first frame, then the frame itself
    assert md5[-8:] == [md5[-8]] * 8  # the last frame, then seven clones
    assert len(set(md5[4:14])) == 10  # the real frames are all there, in order


def test_finalize_upscale_trims_scales_and_keeps_the_rate(tmp_path: Path) -> None:
    vsr = tmp_path / "vsr.mkv"
    _ffv1(vsr, size="640x384", frames=30, rate=25)  # timestamps say 25 fps
    source = _mp4(tmp_path / "s.mp4", frames=24, rate="24000/1001")
    dst = tmp_path / "o.mp4"
    finalize_upscale(
        vsr,
        source,
        dst,
        width=640,
        height=360,
        frames=24,
        rate=Fraction(24000, 1001),
        audio=False,
        comment="vpipe-job:x",
    )
    info = probe_video(dst)
    assert (info.width, info.height, info.frames, info.has_audio) == (640, 360, 24, False)
    assert _fields(dst, "stream=r_frame_rate,color_range,color_space") == {
        "r_frame_rate": "24000/1001",
        "color_range": "tv",
        "color_space": "bt709",
    }
    assert _frame_md5(dst)[0] != _frame_md5(dst)[1]  # stretched, not a still


def test_finalize_upscale_carries_the_source_audio(tmp_path: Path) -> None:
    vsr = tmp_path / "vsr.mkv"
    _ffv1(vsr, size="640x384", frames=24, rate=24)
    source = _mp4(tmp_path / "s.mp4", frames=24, audio=True)
    dst = tmp_path / "o.mp4"
    finalize_upscale(
        vsr, source, dst, width=640, height=360, frames=24, rate=Fraction(24), audio=True,
        comment="x",
    )  # fmt: skip
    streams = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "csv=p=0",
            str(dst),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(streams) == ["aac,audio", "h264,video"]


def _ffv1(path: Path, *, size: str, frames: int, rate: int) -> Path:
    vf = "scale=out_range=pc:out_color_matrix=bt709,format=yuv444p,setparams=range=pc"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate={rate}",
            "-vf",
            vf,
            "-frames:v",
            str(frames),
            "-c:v",
            "ffv1",
            "-color_range",
            "pc",
            str(path),
        ],
        check=True,
    )
    return path


def _frame_md5(path: Path) -> list[str]:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-f", "framemd5", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.rsplit(",", 1)[1].strip() for line in out.splitlines() if not line.startswith("#")]
