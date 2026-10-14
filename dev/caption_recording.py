"""Burn agent-authored, timed captions below product footage using ffmpeg.

See dev/recording-lanes.md for the cue format and evidence review requirements.
Requires ffmpeg with drawtext, ffprobe, and the DejaVu Sans Mono font.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any

DEFAULT_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")


def caption_font() -> Path:
    if DEFAULT_FONT.is_file():
        return DEFAULT_FONT
    if shutil.which("fc-match"):
        match = run(
            ["fc-match", "--format", "%{family}\n%{file}", "DejaVu Sans Mono"], timeout=5
        ).splitlines()
        if len(match) == 2 and match[0] == "DejaVu Sans Mono" and Path(match[1]).is_file():
            return Path(match[1])
    raise ValueError(
        "Install DejaVu Sans Mono (fonts-dejavu-core on Ubuntu); font substitution is disabled"
    )


def run(command: list[str], *, timeout: float = 600, **kwargs: Any) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout, **kwargs
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{command[0]} timed out after {timeout}s") from exc
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stderr.strip()[-4000:]}")
    return result.stdout


def probe(source: Path) -> dict[str, Any]:
    info = json.loads(
        run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(source),
            ],
            timeout=30,
        )
    )
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("Source has no video stream")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("Source video must have known positive dimensions")
    try:
        duration = float(video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if not duration:
        # WebM exposes per-stream duration as a tag; container duration may include longer audio.
        tag = (video.get("tags") or {}).get("DURATION")
        if tag:
            try:
                hours, minutes, seconds = tag.split(":")
                duration = float(hours) * 3600 + float(minutes) * 60 + float(seconds)
            except ValueError:
                duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Source video stream must have a known positive duration")
    return {"width": width, "height": height, "duration": duration}


def validate_cues(value: Any, duration: float, columns: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 200:
        raise ValueError("Cues must be a JSON list of 1–200 {start, end, text} objects")
    cues = []
    previous_end = 0.0
    for index, cue in enumerate(value, 1):
        if not isinstance(cue, dict):
            raise ValueError(f"Cue {index} must be an object")
        start, end, text = cue.get("start"), cue.get("end"), cue.get("text")
        if any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end)):
            raise ValueError(f"Cue {index} needs finite start/end seconds")
        if not 0 <= start < end <= duration or start < previous_end:
            raise ValueError(
                f"Cue {index} must be ordered, non-overlapping, and within the video duration"
            )
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Cue {index} needs nonempty text")
        if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            raise ValueError(f"Cue {index} contains control characters")
        lines = textwrap.wrap(" ".join(text.split()), width=columns)
        if len(lines) > 3:
            raise ValueError(f"Cue {index} exceeds three lines; shorten it or split the step")
        cues.append({"start": start, "end": end, "text": "\n".join(lines)})
        previous_end = end
    return cues


def publish_recording(rendered: Path, output: Path) -> None:
    try:
        os.link(rendered, output)
        return
    except FileExistsError:
        raise
    except OSError:
        pass
    # Filesystems without hard links still need exclusive destination creation.
    with rendered.open("rb") as source:
        destination = output.open("xb")
        try:
            with destination:
                shutil.copyfileobj(source, destination)
        except BaseException:
            output.unlink(missing_ok=True)
            raise


def caption_recording(
    source: Path, cues_path: Path, output: Path, *, render_timeout: float = 600
) -> dict[str, Any]:
    if not math.isfinite(render_timeout) or render_timeout <= 0:
        raise ValueError("Render timeout must be a positive finite number of seconds")
    source, cues_path, output = source.resolve(), cues_path.resolve(), output.resolve()
    if output in (source, cues_path) or output.exists():
        raise ValueError(
            "Use a new output path; source footage and existing renders are never overwritten"
        )
    if output.suffix.lower() != ".mp4":
        raise ValueError("Output must be an .mp4 file")
    info = probe(source)
    width = info["width"] + info["width"] % 2
    height = info["height"] + info["height"] % 2
    if width < 320:
        raise ValueError("Capture at least 320 pixels wide so captions remain legible")
    font_size = max(14, width // 42)
    margin = max(12, font_size // 2)
    # Leave room beyond DejaVu Sans Mono's ~0.6-em advance for glyph overhangs.
    columns = int((width - margin * 2) / (font_size * 0.7))
    cues = validate_cues(
        json.loads(cues_path.read_text(encoding="utf-8")), info["duration"], columns
    )
    band = (font_size * 4 + margin * 2 + 1) // 2 * 2
    output.parent.mkdir(parents=True, exist_ok=True)
    # Fixed relative textfile names avoid ffmpeg filter escaping for paths and
    # text. expansion=none makes expressions such as %{...} literal captions.
    # Keep interrupted renders and the copied font outside the evidence directory.
    with tempfile.TemporaryDirectory(prefix="omnigent-caption-") as directory:
        temporary = Path(directory)
        shutil.copyfile(caption_font(), temporary / "caption-font.ttf")
        filters = [f"pad={width}:{height + band}:0:0:color=0x111827", "setsar=1"]
        for index, cue in enumerate(cues):
            name = f"cue-{index}.txt"
            (temporary / name).write_text(cue["text"], encoding="utf-8")
            filters.append(
                f"drawtext=fontfile=caption-font.ttf:textfile={name}:expansion=none:"
                f"fontsize={font_size}:fontcolor=white:line_spacing=4:"
                f"x={margin}:y={height + margin}:"
                f"enable='gte(t,{cue['start']})*lt(t,{cue['end']})'"
            )
        rendered = temporary / "rendered.mp4"
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-vf",
                ",".join(filters),
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(rendered),
            ],
            cwd=temporary,
            timeout=render_timeout,
        )
        rendered_info = probe(rendered)
        if abs(rendered_info["duration"] - info["duration"]) > 0.25:
            raise ValueError("Caption rendering unexpectedly changed the duration")
        # Publishing must fail if another render created the destination meanwhile.
        publish_recording(rendered, output)
    return {"source": str(source), "output": str(output), "cues": len(cues), **rendered_info}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--cues", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--render-timeout",
        type=float,
        default=600,
        help="Maximum encode time in seconds (default: 600); increase for long recordings",
    )
    args = parser.parse_args()
    try:
        result = caption_recording(
            args.source, args.cues, args.output, render_timeout=args.render_timeout
        )
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Caption rendering failed: {exc}\n")


if __name__ == "__main__":
    main()
