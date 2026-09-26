"""Superfast Decision Gate package.

An optional, off-by-default System One front-door classifier. See
:mod:`omnigent.superfast.decision_gate` for the gate and its shadow-mode
contract.
"""

from __future__ import annotations

from omnigent.superfast.decision_gate import maybe_shadow_gate

__all__ = ["maybe_shadow_gate"]
