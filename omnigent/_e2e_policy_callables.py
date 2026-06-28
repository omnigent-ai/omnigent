"""
E2E-test-only policy callables.

Lives under the ``omnigent`` package so the server
subprocess (which imports from omnigent, not tests/) can
resolve the dotted path. The module itself has no production
value — it exists solely so
``tests/_fixtures/agents/e2e-policy-gate/config.yaml`` can
reference a callable the live server process can import.

Callables receive an event dict and return a decision dict:
``fn(event) -> {"result": ..., "reason": ...}``.

Would not exist in a deployment where agent authors ship
their own policy callables via pip-installed packages.
"""

from __future__ import annotations

from omnigent.policies.schema import PolicyEvent, PolicyResponse
from omnigent.policies.types import PolicyResult
from omnigent.spec.types import PolicyAction

# Deterministic sentinel — arbitrary string unlikely to
# appear in natural user messages, so the e2e test can
# reliably flip the DENY path on / off.
_SENTINEL = "BLOCK_THIS_TOKEN"


def _allow() -> PolicyResponse:
    """Return a fresh ALLOW decision for test policy callables."""
    return {"result": "ALLOW"}


def block_on_sentinel(event: PolicyEvent) -> PolicyResponse:
    """
    DENY any INPUT containing the sentinel token.

    :param event: Event dict. On INPUT phase,
        ``event["data"]`` is the user message text (str).
    :returns: Decision dict — DENY if the sentinel
        appears in the text, ALLOW otherwise.
    """
    content = event.get("data")
    if isinstance(content, str) and _SENTINEL in content:
        return {
            "result": "DENY",
            "reason": f"contains reserved token {_SENTINEL!r}",
        }
    return _allow()


# Trigger token for the e2e-label-gate fixture. When a user
# message contains this string, the FunctionPolicy emits ALLOW
# + a `tainted: "1"` label write. Subsequent turns see the
# label and drive downstream condition gates.
_BANANA_TRIGGER = "BANANA_TRIGGER"


def taint_on_banana(event: PolicyEvent) -> PolicyResult:
    """
    ALLOW every message and emit a label write when the
    input contains the banana-trigger token.

    Returns a native :class:`PolicyResult` (not a decision dict)
    because label writes (``set_labels``) require the
    PolicyResult shape — the decision dict does not carry labels.

    :param event: Event dict.
    :returns: Always ALLOW; carries ``set_labels={"tainted": "1"}``
        when the trigger token appears.
    """
    content = event.get("data")
    if isinstance(content, str) and _BANANA_TRIGGER in content:
        return PolicyResult(
            action=PolicyAction.ALLOW,
            set_labels={"tainted": "1"},
        )
    return PolicyResult(action=PolicyAction.ALLOW)
