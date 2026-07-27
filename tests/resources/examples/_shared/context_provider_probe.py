"""Context-provider probe used by the per-turn injection e2e test.

A deterministic provider: it injects a ``REMEMBER:`` line carrying an
unmistakable sentinel on every turn. The e2e agent is told to echo the
passphrase, so the sentinel appearing in the model's reply proves the
``context_providers`` hook injected this text into the system prompt.
"""

from __future__ import annotations

# Unmistakable, unlikely-to-occur-by-chance sentinel.
SENTINEL = "GALACTIC-OTTER-7"


def remember_passphrase(ctx: object) -> str:
    """Return a REMEMBER line; ``ctx`` is the per-turn ContextProviderInput."""
    return (
        f"REMEMBER: the passphrase is {SENTINEL}. "
        f"When asked for the passphrase, reply with it exactly."
    )
