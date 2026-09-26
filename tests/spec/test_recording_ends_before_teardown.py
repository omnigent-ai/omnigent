"""Recording rules end a web clip with the test body and check its last frame."""

from pathlib import Path

_DEV = Path(__file__).resolve().parents[2] / "dev"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_record_dir_covers_the_sync_page_fixture() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "into pytest-playwright's `page`/`context` fixtures" in lanes


def test_web_lane_ends_the_clip_with_the_test_body() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "The clip ends with the test body, not with fixture teardown" in lanes
    assert (
        "closes the pytest-playwright context as soon as the test body finishes, "
        "before any fixture teardown runs" in lanes
    )
    assert "must close them itself before its body returns" in lanes
    assert "must skip that work when `page.is_closed()`" in lanes


def test_finishing_a_clip_checks_the_last_frame_for_teardown() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "Check the clip's last frame before captioning it" in lanes
    assert "a final frame that shows teardown" in lanes
    assert "means the recording outlived the test body" in lanes
    assert "Fix the stop point and re-record; do not caption around it" in lanes
