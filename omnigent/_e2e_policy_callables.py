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

from omnigent.policies.schema import PolicyEvent, PolicyResponse, request_user_text
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

    :param event: Event dict. On the REQUEST phase the web input gate
        passes ``event["data"]`` as ``{"user_content", "attachments"}``;
        ``request_user_text`` reads the typed text from that dict (and
        still accepts a bare string from the native/terminal path).
    :returns: Decision dict — DENY if the sentinel
        appears in the text, ALLOW otherwise.
    """
    if _SENTINEL in request_user_text(event.get("data")):
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

    :param event: Event dict. On the REQUEST phase the web input gate
        passes ``event["data"]`` as ``{"user_content", "attachments"}``;
        ``request_user_text`` reads the typed text from that dict (and
        still accepts a bare string from the native/terminal path).
    :returns: Always ALLOW; carries ``set_labels={"tainted": "1"}``
        when the trigger token appears.
    """
    if _BANANA_TRIGGER in request_user_text(event.get("data")):
        return PolicyResult(
            action=PolicyAction.ALLOW,
            set_labels={"tainted": "1"},
        )
    return PolicyResult(action=PolicyAction.ALLOW)
