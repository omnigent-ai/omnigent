"""Modular, self-describing harness registry.

This package is the seam introduced by
``designs/harness-modular-registry-proposal.md``. Its goal is that adding or
editing one coding-agent harness eventually touches ONLY that harness's own
files, and that each harness explicitly declares which features it supports.

Phase 0 (this step) introduces the :class:`~omnigent.harnesses.types.HarnessDescriptor`
type and a registry that is a typed *view* derived from the existing scattered
constants (``OMNIGENT_HARNESSES``, ``_HARNESS_MODULES``, ``NATIVE_CODING_AGENTS``,
``HARNESS_ALIASES``, ``_HARNESS_NAME_TO_KEY``, ``harness_supports_model_override``).
Nothing consumes the registry yet — it only adds a single query surface plus a
completeness test that fails loudly if those constants drift out of sync (e.g. a
new harness lands in ``_HARNESS_MODULES`` but not in the spec allowlist). Later
phases invert ownership so those constants derive from the registry instead.
"""

from __future__ import annotations

from omnigent.harnesses.capabilities import (
    AuthModel,
    EffortFamily,
    Elicitation,
    HarnessCapabilities,
    IntegrationMode,
    ModelFamily,
    Resume,
)
from omnigent.harnesses.matrix import render_matrix
from omnigent.harnesses.registry import (
    REGISTRY,
    all_descriptors,
    get,
)
from omnigent.harnesses.types import HarnessDescriptor

__all__ = [
    "REGISTRY",
    "AuthModel",
    "EffortFamily",
    "Elicitation",
    "HarnessCapabilities",
    "HarnessDescriptor",
    "IntegrationMode",
    "ModelFamily",
    "Resume",
    "all_descriptors",
    "get",
    "render_matrix",
]
