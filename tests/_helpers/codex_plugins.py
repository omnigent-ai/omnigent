"""Install caller-named local plugins through the real Codex CLI."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def install_codex_plugin(
    codex: str,
    home: Path,
    workspace: Path,
    marketplace: Path,
    *,
    marketplace_name: str,
    plugin_name: str,
    skill_name: str,
) -> None:
    plugin = marketplace / "plugins" / plugin_name
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": plugin_name, "version": "1.0.0"})
    )
    skill = plugin / "skills" / skill_name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: Local plugin skill\n---\nCheck.\n"
    )
    (marketplace / ".agents/plugins").mkdir(parents=True)
    (marketplace / ".agents/plugins/marketplace.json").write_text(
        json.dumps(
            {
                "name": marketplace_name,
                "owner": {"name": "test"},
                "plugins": [{"name": plugin_name, "source": f"./plugins/{plugin_name}"}],
            }
        )
    )
    for args in [
        ("plugin", "marketplace", "add", str(marketplace)),
        ("plugin", "add", f"{plugin_name}@{marketplace_name}"),
    ]:
        subprocess.run(
            [codex, *args],
            cwd=workspace,
            env={**os.environ, "CODEX_HOME": str(home)},
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
