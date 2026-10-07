"""Decode Playwright video artifacts for recording regression assertions."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image


def ffmpeg() -> str:
    """Playwright's bundled ffmpeg (the one that wrote the video), else one on PATH."""
    default_cache = (
        "~/Library/Caches/ms-playwright" if sys.platform == "darwin" else "~/.cache/ms-playwright"
    )
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or default_cache)
    bundled = sorted(
        candidate
        for candidate in glob.glob(str(root.expanduser() / "ffmpeg-*" / "ffmpeg-*"))
        if os.access(candidate, os.X_OK)
    )
    found = bundled[-1] if bundled else shutil.which("ffmpeg")
    assert found, "no ffmpeg available to decode the recording"
    return found


def fraction_near(frame: Path, color: tuple[int, int, int], tolerance: int = 8) -> float:
    with Image.open(frame) as image:
        data = image.convert("RGB").reduce(8).tobytes()
    pixels = [data[i : i + 3] for i in range(0, len(data), 3)]
    near = sum(
        1
        for p in pixels
        if all(abs(c - want) <= tolerance for c, want in zip(p, color, strict=True))
    )
    return near / len(pixels)


def extract_frames(video: Path, into: Path) -> list[Path]:
    into.mkdir()
    subprocess.run(
        [
            ffmpeg(),
            "-loglevel",
            "error",
            "-i",
            str(video),
            str(into / "%04d.png"),
        ],
        check=True,
        timeout=120,
    )
    frames = sorted(into.glob("*.png"))
    assert frames, f"no frames decoded from {video}"
    return frames
