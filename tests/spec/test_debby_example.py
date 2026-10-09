"""Regression guard for the Debby example's GPT head.

Debby's "GPT" sub-agent must run on the ``codex-native`` harness with an
explicit unsandboxed ``os_env`` (``sandbox.type: none``), like Polly's codex
worker. The SDK ``codex`` harness, routed to a Databricks provider, fails the
signer-backed ``os_env`` sandbox gate (the reported bug); ``openai-agents``
with no pinned model silently routes the head to ambient Databricks
credentials instead of OpenAI. The explicit ``os_env`` is also required:
without it the native Codex terminal defaults to the platform sandbox
(``bwrap`` on Linux) and cannot start on hosts without Bubblewrap.

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


def test_debby_gpt_head_uses_codex_native_unsandboxed() -> None:
    """The GPT head uses ``codex-native`` and keeps the unsandboxed ``os_env``."""
    spec = parse(_DEBBY_DIR)
    by_name = {sub.name: sub for sub in spec.sub_agents}

    assert "gpt" in by_name, f"Debby should declare a 'gpt' sub-agent; got {sorted(by_name)}."
    gpt = by_name["gpt"]

    assert gpt.executor.harness_kind == "codex-native", (
        f"Debby's GPT head must run on the codex-native harness; got "
        f"{gpt.executor.harness_kind!r}. The SDK 'codex' harness needs an "
        f"os_env sandbox on a Databricks host, and 'openai-agents' with no "
        f"pinned model silently routes to ambient Databricks credentials."
    )

    # The native Codex terminal inherits this os_env's sandbox. Without an
    # explicit sandbox: none it defaults to the platform sandbox (bwrap on
    # Linux), so the head must keep the unsandboxed caller-process environment.
    assert gpt.os_env is not None and gpt.os_env.sandbox is not None, (
        "Debby's GPT head must declare an os_env with a sandbox so the native "
        "Codex terminal does not default to the platform sandbox."
    )
    assert gpt.os_env.sandbox.type == "none", (
        f"Debby's GPT head must keep sandbox.type: none; got "
        f"{gpt.os_env.sandbox.type!r}. Any other type requires bwrap/user "
        f"namespaces for the native terminal on Linux hosts."
    )

    # Belt-and-suspenders: the GPT head must not pin a Databricks model or
    # Databricks auth in the spec, which would force the head onto Databricks.
    model = gpt.executor.config.get("model")
    assert model is None or not str(model).startswith("databricks-"), (
        f"Debby's GPT head must not pin a Databricks-hosted model; got {model!r}."
    )
    assert not isinstance(gpt.executor.auth, DatabricksAuth), (
        "Debby's GPT head must not declare Databricks auth in its spec."
    )


def test_packaged_debby_resource_stays_in_sync_with_source_example() -> None:
    """The bundled Debby resource resolves to the updated source example.

    ``omnigent debby`` launches the packaged resource path, not
    ``examples/debby`` directly. Keep this guard so the resource copy cannot
    drift back to ``openai-agents`` or the SDK ``codex`` harness while the
    source example remains fixed.
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
        "Packaged Debby's GPT head must run on the codex-native harness; "
        "bundled launches must match the fixed source example."
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
