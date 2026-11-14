"""OMP executor requires its own bridge rather than borrowing a Pi terminal."""

from pathlib import Path

import pytest

from omnigent.inner.omp_native_executor import OmpNativeExecutor


def test_omp_does_not_fall_back_to_pi_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HARNESS_OMP_NATIVE_BRIDGE_DIR", raising=False)
    monkeypatch.setenv("HARNESS_PI_NATIVE_BRIDGE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="HARNESS_OMP_NATIVE_BRIDGE_DIR"):
        OmpNativeExecutor()


@pytest.mark.parametrize("steering", [False, True])
async def test_omp_image_inputs_are_native_blocks_not_textual_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    steering: bool,
) -> None:
    import json

    executor = OmpNativeExecutor(tmp_path)
    monkeypatch.setattr(executor, "_refresh_auth_headers", lambda: None)
    content = [
        {"type": "input_text", "text": "Describe this"},
        {"type": "input_image", "image_url": "data:image/png;base64,cGl4ZWxz"},
    ]
    if steering:
        assert await executor.enqueue_session_message("main", content)
    else:
        _ = [
            event
            async for event in executor.run_turn(
                [{"role": "user", "content": content}],
                [],
                "",
            )
        ]
    (path,) = (tmp_path / "inbox").glob("*.json")
    queued = json.loads(path.read_text())
    assert queued["content"] == [
        {"type": "text", "text": "Describe this"},
        {"type": "image", "data": "cGl4ZWxz", "mimeType": "image/png"},
    ]


async def test_omp_image_only_input_is_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    executor = OmpNativeExecutor(tmp_path)
    monkeypatch.setattr(executor, "_refresh_auth_headers", lambda: None)
    assert await executor.enqueue_session_message(
        "main",
        [{"type": "input_image", "image_url": "data:image/jpeg;base64,cGl4ZWxz"}],
    )
    (path,) = (tmp_path / "inbox").glob("*.json")
    assert json.loads(path.read_text())["content"] == [
        {"type": "image", "data": "cGl4ZWxz", "mimeType": "image/jpeg"},
    ]


def test_omp_unresolved_image_is_not_silently_lost(tmp_path: Path) -> None:
    executor = OmpNativeExecutor(tmp_path)
    with pytest.raises(ValueError, match="resolved inline image"):
        executor._bridge_content([{"type": "input_image", "file_id": "file_missing"}])
    assert not (tmp_path / "inbox").exists()
