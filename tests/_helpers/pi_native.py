"""Pi own-login catalog seeding for pi-native tests.

Pi keeps its logins in ``auth.json`` and the per-provider model lists it has
fetched in ``models-store.json`` under its agent dir (``~/.pi/agent`` or
``PI_CODING_AGENT_DIR``). The resolver consults both to decide whether a
``provider/model`` reference belongs to Pi's own login; tests seed exactly the
providers and models a scenario needs.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path


def seed_pi_own_login_catalog(agent_dir: Path, provider_id: str, model_ids: Sequence[str]) -> None:
    """Write a Pi agent dir where *provider_id* is logged in and serves *model_ids*.

    :param agent_dir: Pi agent dir to create or extend.
    :param provider_id: Pi provider id, e.g. ``"openai-codex"``.
    :param model_ids: Bare model ids that provider serves, e.g. ``["gpt-5.6-sol"]``.
    """
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "auth.json").write_text(json.dumps({provider_id: {"type": "oauth"}}))
    (agent_dir / "models-store.json").write_text(
        json.dumps({provider_id: {"models": [{"id": model_id} for model_id in model_ids]}})
    )
