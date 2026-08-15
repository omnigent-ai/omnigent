"""Tests for omnigent.agent_bundle — stdlib-only bundle helper."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path


def test_bundle_directory_roundtrip(tmp_path: Path) -> None:
    """bundle_directory tars a directory; files survive the round-trip."""
    (tmp_path / "config.yaml").write_text("name: test-agent\n")
    (tmp_path / "README.md").write_text("hello\n")
    subdir = tmp_path / "skills"
    subdir.mkdir()
    (subdir / "foo.yaml").write_text("skill: foo\n")

    from omnigent.agent_bundle import bundle_directory

    data = bundle_directory(tmp_path)
    assert isinstance(data, bytes)
    assert len(data) > 0

    # Extract and verify entries
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        names = set(tf.getnames())
        assert "config.yaml" in names
        assert "README.md" in names
        assert "skills/foo.yaml" in names

        # Check contents survive verbatim
        content = tf.extractfile("config.yaml").read()
        assert content == b"name: test-agent\n"


def test_bundle_directory_resolved_substitution(tmp_path: Path) -> None:
    """resolved dict overrides specific file content in the tarball."""
    (tmp_path / "config.yaml").write_text("original\n")

    from omnigent.agent_bundle import bundle_directory

    data = bundle_directory(tmp_path, resolved={"config.yaml": "substituted\n"})
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        content = tf.extractfile("config.yaml").read()
        assert content == b"substituted\n"


def test_agent_bundle_does_not_import_cli() -> None:
    """Importing omnigent.agent_bundle must not pull in ``omnigent.cli``.

    The whole point of extracting ``bundle_directory`` out of
    ``omnigent.cli`` is so the host process can import it without the
    heavy CLI stack (the click command groups + the sandbox CLI
    modules). The base ``omnigent`` package ``__init__`` already imports
    click and rich, so a "no click/rich at all" assertion is unachievable
    and not the real goal — what matters is that this module does NOT
    drag in ``omnigent.cli`` itself. Checked in a FRESH interpreter that
    imports only ``omnigent.agent_bundle``, so an unrelated prior import
    in the test session can't mask a regression (an in-process
    ``sys.modules`` check would be polluted by the rest of the suite).
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import omnigent.agent_bundle, sys; "
            "assert callable(omnigent.agent_bundle.bundle_directory); "
            "assert 'omnigent.cli' not in sys.modules, "
            "'agent_bundle must not import omnigent.cli'",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"omnigent.agent_bundle imported omnigent.cli:\n{result.stderr}"
