"""Caption validation and a real encode/decode check of timing and placement."""

import errno
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dev.caption_recording import (
    caption_font,
    caption_recording,
    probe,
    publish_recording,
    run,
    validate_cues,
)


@pytest.mark.parametrize(
    "cues",
    [
        [],
        {},
        ["caption"],
        [{"start": True, "end": 1, "text": "Open"}],
        [{"start": float("nan"), "end": 1, "text": "Open"}],
        [{"start": 0, "end": float("inf"), "text": "Open"}],
        [{"start": -1, "end": 1, "text": "Open"}],
        [{"start": 1, "end": 1, "text": "Open"}],
        [{"start": 0, "end": 5, "text": "Open"}],
        [{"start": 0, "end": 1, "text": "  "}],
        [{"start": 0, "end": 1, "text": "bad\0text"}],
        [{"start": 0, "end": 1, "text": "word " * 100}],
        [
            {"start": 0, "end": 2, "text": "Open"},
            {"start": 1, "end": 3, "text": "Close"},
        ],
    ],
)
def test_invalid_cues_are_rejected(cues):
    with pytest.raises(ValueError):
        validate_cues(cues, duration=4, columns=40)


def test_cues_support_adjacent_intervals_and_literal_text():
    text = "Select 'a:b', then %{n} \\ {x}; → done"
    cues = validate_cues(
        [{"start": 0, "end": 2, "text": text}, {"start": 2, "end": 4, "text": "Done"}],
        duration=4,
        columns=80,
    )
    assert cues[0]["text"] == text
    assert cues[1]["start"] == 2


def test_existing_files_cannot_be_overwritten(tmp_path):
    source = tmp_path / "raw.mp4"
    source.write_bytes(b"original")
    output = tmp_path / "after.mp4"
    output.write_bytes(b"previous render")
    cues = tmp_path / "cues.json"
    for destination in (source, output, cues):
        with pytest.raises(ValueError, match="never overwritten"):
            caption_recording(source, cues, destination)
    assert source.read_bytes() == b"original"
    assert output.read_bytes() == b"previous render"


def _run(command):
    try:
        return subprocess.run(command, check=True, capture_output=True, timeout=60).stdout
    except subprocess.CalledProcessError as exc:
        raise AssertionError(
            f"{command[0]} failed: {exc.stderr.decode(errors='replace')}"
        ) from exc


def _frame(path: Path, seconds: float, crop: str) -> bytes:
    frame = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(seconds),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            crop,
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "-",
        ]
    )
    assert frame, f"No frame decoded at {seconds}s from {path}"
    return frame


@pytest.mark.parametrize("audio_duration", [None, 4, 6], ids=["silent", "audio", "longer-audio"])
@pytest.mark.parametrize("container", ["mp4", "webm"])
def test_render_preserves_footage_audio_and_timing_with_captions_below(
    tmp_path, audio_duration, container
):
    if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
        pytest.skip("Install ffmpeg and ffprobe for the real media check")
    filters = _run(["ffmpeg", "-hide_banner", "-filters"]).decode()
    if "drawtext" not in filters:
        pytest.skip("ffmpeg needs drawtext for the real media check")
    try:
        caption_font()
    except (OSError, ValueError, RuntimeError) as exc:
        pytest.skip(f"Install the caption font for the real media check: {exc}")
    # Special characters in both paths and text must never become filter syntax.
    folder = tmp_path / "a:b's [recording]"
    folder.mkdir()
    source = folder / f"raw.{container}"
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=white:s=640x360:r=10:d=4",
    ]
    if audio_duration:
        command.extend(["-f", "lavfi", "-i", f"sine=frequency=440:duration={audio_duration}"])
    command.extend(
        [
            "-c:v",
            "libx264" if container == "mp4" else "libvpx-vp9",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac" if container == "mp4" else "libopus",
            str(source),
        ]
    )
    _run(command)
    original = source.read_bytes()
    cues = folder / "cues.json"
    cues.write_text(
        json.dumps(
            [
                {"start": 0.5, "end": 1.5, "text": "Open 'a:b' %{n} \\ {x}; →"},
                {"start": 2, "end": 3.9, "text": "The result remains visible."},
            ]
        )
    )
    output = folder / "after-captioned.mp4"
    result = caption_recording(source, cues, output)
    assert source.read_bytes() == original
    assert result["width"] == 640
    assert result["height"] > 360
    assert result["duration"] == pytest.approx(probe(source)["duration"], abs=0.25)
    info = json.loads(
        _run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)])
    )
    assert {s["codec_name"] for s in info["streams"]} == (
        {"h264", "aac"} if audio_duration else {"h264"}
    )
    assert result["duration"] == pytest.approx(4, abs=0.1)
    if audio_duration:
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        assert float(audio["duration"]) == pytest.approx(audio_duration, abs=0.1)
    # Product pixels stay above the band, including the outcome at the end.
    for seconds in (0.2, 1, 1.7, 2.5, 3.8):
        product = _frame(output, seconds, "crop=640:360:0:0")
        assert min(product) > 200
    band = f"crop=640:{result['height'] - 360}:0:360"
    for seconds in (0.2, 1.7):
        assert max(_frame(output, seconds, band)) < 50
    for seconds in (1, 2.5, 3.8):
        assert max(_frame(output, seconds, band)) > 200
    _run(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"])
    if audio_duration == 6:
        cues.write_text('[{"start": 4.5, "end": 5.5, "text": "Too late for the video"}]')
        with pytest.raises(ValueError, match="within the video duration"):
            caption_recording(source, cues, folder / "invalid-captions.mp4")


@pytest.mark.parametrize(
    "tag", ["00:00:01.000000000", None, "1.0", "00:00:01:00", "bad:tag:value"]
)
@pytest.mark.parametrize("duration", [None, "N/A"])
def test_webm_video_duration_does_not_use_longer_container_duration(
    tmp_path, monkeypatch, tag, duration
):
    video = {
        "codec_type": "video",
        "width": 640,
        "height": 360,
        "tags": {"DURATION": tag},
        "duration": duration,
    }
    info = {"streams": [video], "format": {"duration": "4"}}
    monkeypatch.setattr("dev.caption_recording.run", lambda *a, **kw: json.dumps(info))
    if tag == "00:00:01.000000000":
        assert probe(tmp_path / "clip.webm")["duration"] == 1
    else:
        with pytest.raises(ValueError, match="video stream must have a known positive duration"):
            probe(tmp_path / "clip.webm")


def test_failed_render_leaves_raw_footage_and_no_partial_output(tmp_path, monkeypatch):
    source = tmp_path / "raw.webm"
    source.write_bytes(b"source")
    monkeypatch.setattr("dev.caption_recording.caption_font", lambda: source)
    cues = tmp_path / "cues.json"
    cues.write_text('[{"start": 0, "end": 2, "text": "Open"}]')
    monkeypatch.setattr(
        "dev.caption_recording.probe", lambda _: {"width": 640, "height": 360, "duration": 3}
    )

    render_directories = []

    def fail(*args, **kwargs):
        assert kwargs["timeout"] == 900
        directory = Path(kwargs["cwd"])
        assert not directory.is_relative_to(tmp_path)
        render_directories.append(directory)
        raise RuntimeError("drawtext unavailable")

    monkeypatch.setattr("dev.caption_recording.run", fail)
    output = tmp_path / "after.mp4"
    with pytest.raises(RuntimeError, match="drawtext unavailable"):
        caption_recording(source, cues, output, render_timeout=900)
    assert source.read_bytes() == b"source"
    assert not output.exists()
    assert render_directories and all(not path.exists() for path in render_directories)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_unbounded_or_invalid_render_timeout_is_rejected(tmp_path, timeout):
    with pytest.raises(ValueError, match="positive finite"):
        caption_recording(
            tmp_path / "raw.webm",
            tmp_path / "cues.json",
            tmp_path / "out.mp4",
            render_timeout=timeout,
        )


@pytest.mark.parametrize(
    "stream", [None, {}, {"width": 640}, {"height": 360}, {"width": 0, "height": 0}]
)
def test_unprobeable_video_reports_a_captioning_error(tmp_path, monkeypatch, stream):
    info = {} if stream is None else {"streams": [{"codec_type": "video", **stream}]}
    monkeypatch.setattr("dev.caption_recording.run", lambda *a, **kw: json.dumps(info))
    with pytest.raises(ValueError, match="Source"):
        probe(tmp_path / "broken.mp4")


def test_media_command_timeout_is_reported(monkeypatch):
    def stalled(command, **kwargs):
        assert kwargs["timeout"] == 0.01
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("dev.caption_recording.subprocess.run", stalled)
    with pytest.raises(RuntimeError, match=r"ffmpeg timed out after 0\.01s"):
        run(["ffmpeg"], timeout=0.01)


def test_concurrent_output_is_never_overwritten(tmp_path, monkeypatch):
    source = tmp_path / "raw.webm"
    source.write_bytes(b"source")
    monkeypatch.setattr("dev.caption_recording.caption_font", lambda: source)
    cues = tmp_path / "cues.json"
    cues.write_text('[{"start": 0, "end": 2, "text": "Open"}]')
    output = tmp_path / "after.mp4"
    monkeypatch.setattr(
        "dev.caption_recording.probe", lambda _: {"width": 640, "height": 360, "duration": 3}
    )

    render_directories = []

    def concurrent_render(command, **kwargs):
        render_directories.append(Path(kwargs["cwd"]))
        Path(command[-1]).write_bytes(b"our render")
        output.write_bytes(b"another render")

    monkeypatch.setattr("dev.caption_recording.run", concurrent_render)
    with pytest.raises(FileExistsError):
        caption_recording(source, cues, output)
    assert output.read_bytes() == b"another render"
    assert source.read_bytes() == b"source"
    assert render_directories and all(not path.exists() for path in render_directories)


@pytest.mark.parametrize("failure", [None, "access", "exists", "copy", "close"])
def test_publish_without_hardlinks_preserves_files_on_failure(tmp_path, monkeypatch, failure):
    rendered = tmp_path / "rendered.mp4"
    rendered.write_bytes(b"encoded video")
    output = tmp_path / "after.mp4"

    def unsupported(*args):
        raise OSError(
            errno.EACCES if failure == "access" else errno.EOPNOTSUPP, "Hard links unavailable"
        )

    monkeypatch.setattr("dev.caption_recording.os.link", unsupported)
    if failure == "exists":
        output.write_bytes(b"another render")
        with pytest.raises(FileExistsError):
            publish_recording(rendered, output)
        assert output.read_bytes() == b"another render"
    elif failure == "copy":

        def fail_copy(source, destination):
            destination.write(b"partial")
            raise OSError("Disk full")

        monkeypatch.setattr("dev.caption_recording.shutil.copyfileobj", fail_copy)
        with pytest.raises(OSError, match="Disk full"):
            publish_recording(rendered, output)
        assert not output.exists()
    elif failure == "close":

        def fail_close(source, destination):
            destination.write(b"buffered")
            # An unflushable buffer must be cleaned up when the context closes.
            os.close(destination.fileno())

        monkeypatch.setattr("dev.caption_recording.shutil.copyfileobj", fail_close)
        with pytest.raises(OSError):
            publish_recording(rendered, output)
        assert not output.exists()
    else:
        publish_recording(rendered, output)
        assert output.read_bytes() == b"encoded video"
    assert rendered.read_bytes() == b"encoded video"


@pytest.mark.parametrize("family", ["DejaVu Sans Mono", "DejaVu Sans"])
def test_font_lookup_rejects_silent_font_substitution(tmp_path, monkeypatch, family):
    font = tmp_path / "font.ttf"
    font.write_bytes(b"font fixture")
    monkeypatch.setattr("dev.caption_recording.DEFAULT_FONT", tmp_path / "not-installed.ttf")
    monkeypatch.setattr("dev.caption_recording.shutil.which", lambda _: "/usr/bin/fc-match")
    monkeypatch.setattr("dev.caption_recording.run", lambda *a, **kw: f"{family}\n{font}")
    if family == "DejaVu Sans Mono":
        assert caption_font() == font
    else:
        with pytest.raises(ValueError, match="font substitution is disabled"):
            caption_font()
