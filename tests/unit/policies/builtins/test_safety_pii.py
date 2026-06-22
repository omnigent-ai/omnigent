"""
Tests for ``deny_pii_in_llm_request`` built-in policy
(:mod:`omnigent.policies.builtins.safety`).

Covers:

- Default patterns (SSN, credit card, email, phone) fire on
  ``llm_request`` events when PII is present in the system prompt.
- Clean prompts return ALLOW.
- Non-``llm_request`` events always return ALLOW (phase filter).
- Custom patterns override defaults.
- Empty patterns dict disables all scanning.
- ``action`` parameter controls DENY vs ASK verdict.
"""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.safety import deny_pii_in_llm_request
from tests.unit.policies.builtins.helpers import llm_request_event, tool_call_event

# ── Default patterns ─────────────────────────────────────────────────────────


def test_denies_ssn_in_system_prompt() -> None:
    """An SSN pattern (123-45-6789) in the system prompt triggers DENY.

    If this returns ALLOW, the SSN regex is broken or the data
    field is not being read correctly.
    """
    policy = deny_pii_in_llm_request()
    event = llm_request_event(
        system_prompt_preview="Customer SSN is 123-45-6789, handle with care."
    )
    result = policy(event)
    assert result["result"] == "DENY", (
        "SSN in system prompt should be denied. "
        "If ALLOW, the SSN regex didn't match or the wrong field was scanned."
    )
    assert "social security" in result["reason"].lower(), (
        "Reason should name the PII category so the admin knows what triggered it."
    )


def test_denies_email_in_system_prompt() -> None:
    """An email address in the system prompt triggers DENY.

    If this returns ALLOW, the email regex is not matching.
    """
    policy = deny_pii_in_llm_request()
    event = llm_request_event(system_prompt_preview="Contact alice@example.com for support.")
    result = policy(event)
    assert result["result"] == "DENY", "Email in system prompt should be denied."
    assert "email" in result["reason"].lower()


def test_denies_phone_in_system_prompt() -> None:
    """A US phone number in the system prompt triggers DENY.

    If this returns ALLOW, the phone regex is not matching.
    """
    policy = deny_pii_in_llm_request()
    event = llm_request_event(system_prompt_preview="Call the user at (555) 123-4567.")
    result = policy(event)
    assert result["result"] == "DENY"
    assert "phone" in result["reason"].lower()


@pytest.mark.parametrize(
    "phone",
    ["+44 20 7946 0958", "+81 3-1234-5678", "090-1234-5678", "07911 123456"],
    ids=["intl-uk", "intl-jp", "jp-mobile", "uk-mobile"],
)
def test_denies_international_phone_numbers(phone: str) -> None:
    """Phone numbers from various countries are caught by
    the single ``phone`` category.

    :param phone: Phone number string to embed in the prompt.
    """
    policy = deny_pii_in_llm_request()
    event = llm_request_event(system_prompt_preview=f"Call the user at {phone}.")
    result = policy(event)
    assert result["result"] == "DENY", f"Phone '{phone}' should be denied."


def test_allows_clean_system_prompt() -> None:
    """A system prompt with no PII passes through with ALLOW.

    If this returns DENY, a pattern is over-matching on benign text.
    """
    policy = deny_pii_in_llm_request()
    event = llm_request_event(system_prompt_preview="You are a helpful coding assistant.")
    result = policy(event)
    assert result["result"] == "ALLOW", (
        "Clean prompt should be allowed. If DENY, a PII pattern is matching non-PII text."
    )


# ── REQUEST phase (universal, all harnesses) ─────────────────────────────────


def test_denies_email_in_request_phase() -> None:
    """PII in a ``request`` event (user input) triggers DENY.

    The ``request`` phase fires for every harness (including
    supervisor, native) without needing the harness-level
    LLM_REQUEST callback. If this returns ALLOW, the request-
    phase branch is broken and PII passes through on harnesses
    without the callback.
    """
    policy = deny_pii_in_llm_request()
    event = {
        "type": "request",
        "target": None,
        "data": "my email is tomu.hirata@gmail.com",
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "DENY", (
        "Email in request-phase user message should be denied. "
        "If ALLOW, the request-phase branch is not scanning text."
    )
    assert "email" in result["reason"].lower()


def test_allows_clean_request_phase() -> None:
    """A clean user message at the request phase passes through."""
    policy = deny_pii_in_llm_request()
    event = {
        "type": "request",
        "target": None,
        "data": "write me a hello world program",
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "ALLOW"


def test_denies_ssn_in_request_phase() -> None:
    """SSN in a ``request`` event triggers DENY."""
    policy = deny_pii_in_llm_request()
    event = {
        "type": "request",
        "target": None,
        "data": "my SSN is 123-45-6789",
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "DENY"


# ── LLM_REQUEST phase (user message field) ───────────────────────────────────


def test_denies_email_in_user_message() -> None:
    """PII in the user message (not just system prompt) triggers DENY.

    This is the primary attack vector — users typing PII into the
    chat. If this returns ALLOW, the policy is only scanning the
    system prompt and missing user content entirely.
    """
    policy = deny_pii_in_llm_request()
    event = llm_request_event(
        system_prompt_preview="You are a helpful assistant.",
        last_user_message="my email is tomu.hirata@gmai.com",
    )
    result = policy(event)
    assert result["result"] == "DENY", (
        "Email in user message should be denied. If ALLOW, last_user_message is not being scanned."
    )
    assert "email" in result["reason"].lower()


def test_denies_ssn_in_user_message() -> None:
    """SSN in the user message triggers DENY."""
    policy = deny_pii_in_llm_request()
    event = llm_request_event(
        last_user_message="my SSN is 123-45-6789",
    )
    result = policy(event)
    assert result["result"] == "DENY"


def test_clean_user_message_allows() -> None:
    """A user message with no PII passes through."""
    policy = deny_pii_in_llm_request()
    event = llm_request_event(
        last_user_message="write me a hello world program",
    )
    result = policy(event)
    assert result["result"] == "ALLOW"


# ── Phase filtering ──────────────────────────────────────────────────────────


def test_ignores_non_llm_request_events() -> None:
    """The policy only fires on ``llm_request``. Tool calls pass through.

    If this returns DENY, the phase filter is broken.
    """
    policy = deny_pii_in_llm_request()
    # A tool call event with PII-like content should not be intercepted.
    event = tool_call_event("Bash", {"command": "echo 123-45-6789"})
    result = policy(event)
    assert result["result"] == "ALLOW", (
        "Non-llm_request events should always ALLOW regardless of content."
    )


# ── Selective PII types ──────────────────────────────────────────────────────


def test_selecting_only_ssn_ignores_email() -> None:
    """When only ``ssn`` is selected, email addresses pass through.

    If this returns DENY, the pii_types filter is not working —
    unselected categories are leaking through.
    """
    policy = deny_pii_in_llm_request(pii_types=["ssn"])

    # SSN should match.
    event_ssn = llm_request_event(system_prompt_preview="SSN is 123-45-6789")
    assert policy(event_ssn)["result"] == "DENY"

    # Email should NOT match — only ssn is selected.
    event_email = llm_request_event(system_prompt_preview="Contact alice@example.com")
    result_email = policy(event_email)
    assert result_email["result"] == "ALLOW", (
        "Only 'ssn' was selected; email should pass through. "
        "If DENY, the pii_types filter is not restricting categories."
    )


def test_empty_pii_types_enables_all() -> None:
    """An empty ``pii_types`` list enables all built-in categories.

    This is the default behavior — admins who leave the field
    empty get full coverage.
    """
    policy = deny_pii_in_llm_request(pii_types=[])
    event = llm_request_event(system_prompt_preview="SSN 123-45-6789")
    assert policy(event)["result"] == "DENY", (
        "Empty pii_types should enable all categories. "
        "If ALLOW, the empty-list → all-categories fallback is broken."
    )


def test_none_pii_types_enables_all() -> None:
    """``pii_types=None`` (default) enables all built-in categories."""
    policy = deny_pii_in_llm_request(pii_types=None)
    event = llm_request_event(system_prompt_preview="Email: alice@test.com")
    assert policy(event)["result"] == "DENY"


def test_unknown_pii_type_silently_ignored() -> None:
    """Unknown category keys are silently ignored.

    If this raises, the factory is not filtering unknown keys.
    """
    policy = deny_pii_in_llm_request(pii_types=["nonexistent_type"])
    event = llm_request_event(system_prompt_preview="SSN 123-45-6789 email alice@test.com")
    # No valid categories selected → nothing to match → ALLOW.
    assert policy(event)["result"] == "ALLOW"


# ── Action parameter ─────────────────────────────────────────────────────────


def test_action_ask_returns_ask_on_match() -> None:
    """``action="ASK"`` emits ASK instead of DENY on PII match.

    Useful when admins want human review rather than hard block.
    """
    policy = deny_pii_in_llm_request(action="ASK")
    event = llm_request_event(system_prompt_preview="SSN is 123-45-6789")
    result = policy(event)
    assert result["result"] == "ASK", (
        "action='ASK' should emit ASK, not DENY. "
        "If DENY, the action parameter is not being threaded through."
    )


# ── Edge cases ───────────────────────────────────────────────────────────────


def test_empty_system_prompt_allows() -> None:
    """An empty system_prompt_preview is allowed (nothing to scan).

    If this raises or returns DENY, the empty-string edge is mishandled.
    """
    policy = deny_pii_in_llm_request()
    event = llm_request_event(system_prompt_preview="")
    result = policy(event)
    assert result["result"] == "ALLOW"


@pytest.mark.parametrize(
    "ssn",
    ["123-45-6789", "000-00-0000", "999-99-9999"],
    ids=["typical", "zeros", "nines"],
)
def test_ssn_variations(ssn: str) -> None:
    """Multiple SSN formats are caught by the default pattern.

    :param ssn: SSN string to embed in the prompt.
    """
    policy = deny_pii_in_llm_request()
    event = llm_request_event(system_prompt_preview=f"The number is {ssn}.")
    result = policy(event)
    assert result["result"] == "DENY", f"SSN '{ssn}' should be caught by the default pattern."
