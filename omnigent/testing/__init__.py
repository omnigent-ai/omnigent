"""Test-environment safety helpers for the Omnigent suite.

Houses additive guardrails that assert a test run is pointed at
throwaway resources (a tmp/in-memory SQLite DB, no dev/prod ports)
rather than a developer's real local instance. See
:mod:`omnigent.testing.guardrails`.
"""

from __future__ import annotations
