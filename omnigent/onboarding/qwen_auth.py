"""Shared Qwen credential-state inspection."""

from __future__ import annotations

import json
import os


def qwen_auth_configured() -> bool:
    """Best-effort check whether Qwen Code can authenticate non-interactively.

    Qwen has **no CLI login** — its ``auth`` subcommand was removed. For our
    ``qwen --acp`` executor, auth must come from one of:

    - API-key / provider env vars (the headless path): ``OPENAI_API_KEY``,
      ``BAILIAN_CODING_PLAN_API_KEY``, or ``OPENROUTER_API_KEY``; or
    - an auth type selected via the interactive ``/auth`` flow (API key or the
      Alibaba Cloud Coding Plan), persisted to ``~/.qwen/settings.json``.

    (Qwen OAuth was discontinued on 2026-04-15, so it is not an auth path here.)

    Best-effort: the env-var check is reliable; the on-disk check keys off
    ``settings.json`` fields whose schema is not contract-stable (see
    docs/QWEN_FOLLOWUPS.md). Returns ``False`` for a fresh install with no auth —
    the case that must NOT render as "signed in".

    :returns: ``True`` when auth is detectable, else ``False``.
    """
    from pathlib import Path

    if any(
        os.environ.get(v)
        for v in ("OPENAI_API_KEY", "BAILIAN_CODING_PLAN_API_KEY", "OPENROUTER_API_KEY")
    ):
        return True
    settings = Path.home() / ".qwen" / "settings.json"
    if settings.is_file():
        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            return False
        if isinstance(data, dict):
            if data.get("selectedAuthType"):
                return True
            security = data.get("security")
            auth = security.get("auth") if isinstance(security, dict) else None
            if isinstance(auth, dict) and (
                auth.get("selectedType") or auth.get("selectedAuthType")
            ):
                return True
    return False
