"""
Tests for the environments wrapper package.

The wrapper is pure re-exports of ``omnigent.inner.os_env`` and
the ``OSEnvSpec`` / ``OSEnvSandboxSpec`` dataclasses from
``omnigent.inner.datamodel``. Tests only verify two properties:
every name in ``__all__`` resolves on the wrapper module, and the
re-exported objects are identity-equal to their inner counterparts.
Behavioral coverage of ``OSEnvironment`` and the helper subprocess
lives in ``tests/inner/`` and continues to exercise ``inner.os_env``
directly.
"""

from __future__ import annotations

import omnigent.inner.datamodel as inner_dm
import omnigent.inner.os_env as inner_os_env
from omnigent import environments


def test_environments_all_symbols_importable() -> None:
    """
    Every name in ``omnigent.environments.__all__`` resolves on the
    wrapper module.

    Catches accidental drift between the ``__all__`` list and the actual
    re-exports — without this, a typo would slip through silently and only
    surface at the first downstream import.
    """
    for name in environments.__all__:
        assert hasattr(environments, name), f"omnigent.environments missing re-export {name!r}"


def test_environments_reexports_inner_os_env_objects() -> None:
    """
    The OS-environment classes and factories re-export the same Python
    objects as ``omnigent.inner.os_env``.

    Identity (``is``) — not equality — because anything else means a
    parallel definition has been introduced. Subclass/isinstance checks
    against the wrapper-imported names must continue to match instances
    produced via the inner module.
    """
    assert environments.OSEnvironment is inner_os_env.OSEnvironment
    assert environments.CallerProcessOSEnvironment is inner_os_env.CallerProcessOSEnvironment
    assert environments.create_os_environment is inner_os_env.create_os_environment
    assert environments.default_os_env_spec_for_type is inner_os_env.default_os_env_spec_for_type


def test_environments_reexports_inner_datamodel_specs() -> None:
    """
    ``OSEnvSpec`` and ``OSEnvSandboxSpec`` re-export the same dataclasses
    as ``omnigent.inner.datamodel``.

    Identity matters because these are dataclasses consumers instantiate
    directly (``OSEnvSpec(type="caller_process", ...)``) — a parallel copy
    would mean instances built via the wrapper would not satisfy
    ``isinstance`` checks inside inner code that still reaches for the
    original class.
    """
    assert environments.OSEnvSpec is inner_dm.OSEnvSpec
    assert environments.OSEnvSandboxSpec is inner_dm.OSEnvSandboxSpec


def test_default_os_env_spec_for_type_returns_caller_process() -> None:
    """
    ``default_os_env_spec_for_type("caller_process")`` returns an
    ``OSEnvSpec`` with the matching type discriminator.

    A minimal smoke test confirming the re-exported factory is genuinely
    callable through the wrapper (not just a name lookup) and produces a
    spec instance from the wrapper's ``OSEnvSpec`` class — proves the two
    re-exports work together end-to-end.
    """
    spec = environments.default_os_env_spec_for_type("caller_process")
    assert isinstance(spec, environments.OSEnvSpec)
    assert spec.type == "caller_process"
