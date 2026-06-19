"""Regression guard for the Debby example's GPT head.

Debby's "GPT" sub-agent must run on the ``codex-native`` harness (the real
Codex CLI), not ``openai-agents``. The openai-agents harness treats an unpinned
model as a Databricks model (``is_databricks_model = model is None`` in
``omnigent/inner/openai_agents_sdk_executor.py``) and, with no
``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` in the environment, silently falls
back to ambient Databricks credentials — routing the "GPT" head through the
Databricks gateway. codex-native has no such unpinned-model -> Databricks
default: it is GPT-only and, absent a Databricks provider, authenticates
through the Codex CLI login (ChatGPT subscription). (Like any harness it would
still honor an explicitly-configured or ambient Databricks provider if one were
set; it just never picks Databricks merely because the model is unpinned.)

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


def test_debby_gpt_head_uses_codex_native_not_openai_agents() -> None:
    """The GPT head runs on ``codex-native`` with no unpinned-model -> Databricks default.

    If this flips back to ``openai-agents`` with no pinned model, Debby's GPT
    head falls back to ambient Databricks credentials for any user with a
    Databricks profile configured — the exact bug this example was fixed for.
    codex-native has no such default. This per-head guard only rules out a
    per-head Databricks auth declaration and a pinned Databricks model — it does
    not (and cannot) prevent a globally-configured or ambient Databricks
    provider.
    """
    spec = parse(_DEBBY_DIR)
    by_name = {sub.name: sub for sub in spec.sub_agents}

    assert "gpt" in by_name, f"Debby should declare a 'gpt' sub-agent; got {sorted(by_name)}."
    gpt = by_name["gpt"]

    assert gpt.executor.harness_kind == "codex-native", (
        f"Debby's GPT head must run on the 'codex-native' harness; got "
        f"{gpt.executor.harness_kind!r}. 'openai-agents' with no pinned model "
        f"silently falls back to ambient Databricks credentials."
    )

    # The Polly-compatible native field must be present, not just the harness
    # name — a headless head can't answer Codex approval prompts. The spec
    # parser str-coerces config values, so ``yolo: true`` arrives as the string
    # ``"True"``; compare the lowercased spelling.
    assert str(gpt.executor.config.get("yolo")).strip().lower() == "true", (
        "Debby's GPT head must set ``yolo: true`` so the codex-native CLI runs "
        "with full approval/sandbox bypass (headless heads can't answer prompts)."
    )

    # Belt-and-suspenders: the GPT head must not itself pin a Databricks model
    # or declare Databricks auth (the example ships no Databricks provider, so
    # codex-native defaults to the Codex CLI login).
    model = gpt.executor.config.get("model")
    assert model is None or not str(model).startswith("databricks-"), (
        f"Debby's GPT head must not pin a Databricks-hosted model; got {model!r}."
    )
    assert not isinstance(gpt.executor.auth, DatabricksAuth), (
        "Debby's GPT head must not declare Databricks auth — the example leaves "
        "the GPT head on the Codex CLI login by default."
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
    assert by_name["gpt"].executor.harness_kind == "codex-native", (
        "Packaged Debby's GPT head must run on the 'codex-native' harness; "
        "bundled launches must not fall back to openai-agents."
    )


def test_debby_claude_head_uses_claude_native() -> None:
    """The Claude head runs on ``claude-native`` (the real Claude Code CLI)."""
    spec = parse(_DEBBY_DIR)
    by_name = {sub.name: sub for sub in spec.sub_agents}

    assert "claude" in by_name, (
        f"Debby should declare a 'claude' sub-agent; got {sorted(by_name)}."
    )
    claude = by_name["claude"]
    assert claude.executor.harness_kind == "claude-native", (
        "Debby's Claude head should run on the 'claude-native' harness."
    )
    # The Polly-compatible native field must be present, not just the harness
    # name — managed Claude settings disable bypassPermissions, so a headless
    # head needs ``permission_mode: auto`` to auto-approve without prompting.
    assert claude.executor.config.get("permission_mode") == "auto", (
        "Debby's Claude head must set ``permission_mode: auto`` so the "
        "claude-native CLI auto-approves without prompting (headless heads "
        "can't answer ApprovalCards)."
    )
