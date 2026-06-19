"""Regression guard for the Debby example's GPT head.

Debby's "GPT" sub-agent must run on the ``codex`` harness, not
``openai-agents``. The openai-agents harness treats an unpinned model as a
Databricks model (``is_databricks_model = model is None`` in
``omnigent/inner/openai_agents_sdk_executor.py``) and, with no
``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` in the environment, silently falls
back to ambient Databricks credentials — routing the "GPT" head through the
Databricks gateway instead of OpenAI. The ``codex`` harness is GPT-only, uses
OpenAI's native auth, and has no such unpinned-model Databricks fallback.

This is a non-live parse-only check so it runs in the default suite (the
dir-shaped example's own e2e coverage lives under ``tests/e2e``, which is
ignored by default).
"""

from __future__ import annotations

from pathlib import Path

from omnigent.spec.parser import parse
from omnigent.spec.types import DatabricksAuth

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEBBY_DIR = _REPO_ROOT / "examples" / "debby"
_PACKAGED_DEBBY_DIR = _REPO_ROOT / "omnigent" / "resources" / "examples" / "debby"


def test_debby_gpt_head_uses_codex_not_openai_agents() -> None:
    """The GPT head runs on ``codex`` and never silently routes to Databricks.

    If this flips back to ``openai-agents`` with no pinned model, Debby's GPT
    head falls back to ambient Databricks credentials for any user with a
    Databricks profile configured — the exact bug this example was fixed for.
    """
    spec = parse(_DEBBY_DIR)
    by_name = {sub.name: sub for sub in spec.sub_agents}

    assert "gpt" in by_name, f"Debby should declare a 'gpt' sub-agent; got {sorted(by_name)}."
    gpt = by_name["gpt"]

    assert gpt.executor.harness_kind == "codex", (
        f"Debby's GPT head must run on the 'codex' harness; got "
        f"{gpt.executor.harness_kind!r}. 'openai-agents' with no pinned model "
        f"silently falls back to ambient Databricks credentials."
    )

    # Belt-and-suspenders: the GPT head must not pin a Databricks model or
    # Databricks auth, so it can only resolve the OpenAI/Codex provider.
    model = gpt.executor.config.get("model")
    assert model is None or not str(model).startswith("databricks-"), (
        f"Debby's GPT head must not pin a Databricks-hosted model; got {model!r}."
    )
    assert not isinstance(gpt.executor.auth, DatabricksAuth), (
        "Debby's GPT head must not declare Databricks auth — it should route "
        "to OpenAI via the codex harness."
    )


def test_packaged_debby_resource_stays_in_sync_with_source_example() -> None:
    """The bundled Debby resource resolves to the updated source example.

    ``omnigent debby`` launches the packaged resource path, not
    ``examples/debby`` directly. Keep this guard so the resource copy cannot
    drift back to ``openai-agents`` while the source example remains fixed.
    """
    assert _PACKAGED_DEBBY_DIR.exists(), "Debby's packaged resource should exist."
    assert _PACKAGED_DEBBY_DIR.resolve() == _DEBBY_DIR.resolve(), (
        "Debby's packaged resource must resolve to examples/debby so bundled "
        "launches use the same GPT-head config as the source example."
    )

    spec = parse(_PACKAGED_DEBBY_DIR)
    by_name = {sub.name: sub for sub in spec.sub_agents}

    assert "gpt" in by_name, (
        f"Packaged Debby should declare a 'gpt' sub-agent; got {sorted(by_name)}."
    )
    assert by_name["gpt"].executor.harness_kind == "codex", (
        "Packaged Debby's GPT head must run on the 'codex' harness; bundled "
        "launches must not fall back to openai-agents."
    )


def test_debby_claude_head_unchanged() -> None:
    """The Claude head still runs on ``claude-sdk`` (the fix is GPT-only)."""
    spec = parse(_DEBBY_DIR)
    by_name = {sub.name: sub for sub in spec.sub_agents}

    assert "claude" in by_name, (
        f"Debby should declare a 'claude' sub-agent; got {sorted(by_name)}."
    )
    assert by_name["claude"].executor.harness_kind == "claude-sdk", (
        "Debby's Claude head should remain on the 'claude-sdk' harness."
    )
