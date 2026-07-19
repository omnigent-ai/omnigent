"""Encoding-policy guards for omnigent-owned text I/O.

Windows defaults text I/O to cp1252, so an ``open``/``read_text``/``write_text``
without an explicit ``encoding`` mojibakes UTF-8 content (e.g. an em-dash in an
agent prompt). Ruff's ``PLW1514`` catches the statically-inferable cases; these
tests add a runtime net that also covers reads whose receiver type Ruff can't
infer, and a Windows end-to-end regression.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from omnigent.spec.parser import parse


def test_agent_loading_emits_no_encoding_warning(tmp_path: Path) -> None:
    """Cross-platform net: loading an agent config under
    ``-X warn_default_encoding -W error::EncodingWarning`` must not trip any
    unspecified-encoding read — catching non-inferable reads PLW1514 misses,
    regardless of the CI host's locale.
    """
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "config.yaml").write_text(
        'spec_version: 1\nname: t\nprompt: "plans — not code ↔ review"\n',
        encoding="utf-8",
    )
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        from omnigent.spec.parser import parse
        spec = parse(Path(r{str(agent)!r}))
        assert spec.instructions and "—" in spec.instructions
        """
    )
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "warn_default_encoding",
            "-W",
            "error::EncodingWarning",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.windows_only
def test_config_prompt_survives_cp1252_default(tmp_path: Path) -> None:
    """On Windows (cp1252 default), a UTF-8 config's non-ASCII prompt is read as
    UTF-8 rather than mojibaked (regression for the ``itself — it plans`` phrase).
    """
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "config.yaml").write_text(
        'spec_version: 1\nname: t\nprompt: "itself — it plans ↔ delegates"\n',
        encoding="utf-8",
    )
    spec = parse(agent)
    assert "itself — it plans" in (spec.instructions or "")
    assert "â€" not in (spec.instructions or "")
