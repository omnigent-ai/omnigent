"""
Harness package — per-conversation subprocesses that implement a
subset of the Omnigent REST API.

See ``designs/SERVER_HARNESS_CONTRACT.md`` for the full contract.
The harness IS an HTTP service speaking the same Pydantic models AP
serves to external clients (re-use ``omnigent.server.schemas`` —
there is no separate protocol module).

This package contains:

- ``_HARNESS_MODULES``: registry mapping harness name (the value of
  ``spec.executor.harness`` in an agent spec) to the fully-qualified
  Python module path that exports a zero-argument ``create_app() ->
  FastAPI``. Populated as per-harness wraps land (Phase 1 step 4).
- ``process_manager``: ``HarnessProcessManager`` — owns
  per-conversation subprocess lifecycle.
- ``_runner``: shared ``python -m`` entrypoint that any registered
  harness's ``create_app()`` is served through.

The package directory is intentionally small. Behavior lives in the
sibling modules; this ``__init__.py`` is just the registry.
"""

from __future__ import annotations

from omnigent.runtime.harness_descriptors import runtime_module_map

# Harness-name → fully-qualified module path. Each module must
# export ``create_app() -> FastAPI``; the runner imports the module,
# calls the factory, and serves the result over a Unix socket.
#
# Derived from the single
# :data:`~omnigent.runtime.harness_descriptors.HARNESS_DESCRIPTORS`
# registration (runtime-registered descriptors plus their runtime aliases,
# e.g. ``claude`` → claude-sdk). ``open-responses`` is intentionally absent
# (it is spec-valid but routed through the openai-agents adapter, so its
# descriptor sets ``runtime_registered=False``). The conformance suite
# (``tests/harness_conformance``) asserts this map equals the descriptors.
#
# The test suite injects fixture entries at test time (via direct dict
# mutation in conftest fixtures).
_HARNESS_MODULES: dict[str, str] = runtime_module_map()

__all__ = ["_HARNESS_MODULES"]
