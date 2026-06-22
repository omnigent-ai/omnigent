"""
Tests for the sandbox wrapper package.

The wrapper is pure re-exports of the existing
``omnigent.inner.sandbox`` surface, so the tests only verify two
properties: every name the ``__all__`` lists is importable, and the
wrapped names are the same Python objects as their inner
counterparts. Behavioral tests for the underlying implementation live
in ``tests/inner/`` and continue to exercise ``inner.sandbox``
directly.
"""

from __future__ import annotations

import omnigent.inner.bwrap_sandbox as inner_bwrap
import omnigent.inner.sandbox as inner_sandbox
from omnigent import sandbox
from omnigent.sandbox import bwrap


def test_sandbox_all_symbols_importable() -> None:
    """
    Every name in ``omnigent.sandbox.__all__`` resolves to an attribute on
    the wrapper module.

    Catches accidental drift between the ``__all__`` list and the actual
    re-export — the failure mode is "looks fine in the source, breaks at
    `from omnigent.sandbox import X`".
    """
    for name in sandbox.__all__:
        assert hasattr(sandbox, name), f"omnigent.sandbox missing re-export {name!r}"


def test_sandbox_reexports_are_inner_objects() -> None:
    """
    The re-exported symbols are the same Python objects as the originals
    in ``omnigent.inner.sandbox``.

    Identity (``is``) — not equality — because anything else means the
    wrapper has accidentally introduced a parallel definition. Drift here
    would silently break behavior since callers using either import path
    would get different classes and ``isinstance`` checks would fail.
    """
    assert sandbox.SandboxPolicy is inner_sandbox.SandboxPolicy
    assert sandbox.SandboxBackend is inner_sandbox.SandboxBackend
    assert sandbox.resolve_sandbox is inner_sandbox.resolve_sandbox
    assert sandbox.activate_sandbox is inner_sandbox.activate_sandbox
    assert sandbox.register_backend is inner_sandbox.register_backend
    assert sandbox.with_additional_read_roots is inner_sandbox.with_additional_read_roots
    assert sandbox.with_additional_write_files is inner_sandbox.with_additional_write_files
    assert sandbox.with_additional_write_roots is inner_sandbox.with_additional_write_roots
    assert sandbox.create_private_tmpdir is inner_sandbox.create_private_tmpdir
    assert sandbox.cleanup_private_tmpdir is inner_sandbox.cleanup_private_tmpdir
    assert sandbox.set_temp_env is inner_sandbox.set_temp_env
    assert sandbox.run_launcher is inner_sandbox.run_launcher
    assert sandbox.create_exec_launcher is inner_sandbox.create_exec_launcher


def test_bwrap_all_symbols_importable() -> None:
    """
    Every name in ``omnigent.sandbox.bwrap.__all__`` resolves on the
    submodule.

    The bwrap wrapper exists for re-export + side-effecting
    registration, so a typo in ``__all__`` would silently break
    consumers.
    """
    for name in bwrap.__all__:
        assert hasattr(bwrap, name), f"omnigent.sandbox.bwrap missing re-export {name!r}"


def test_bwrap_reexports_are_inner_objects() -> None:
    """
    ``BwrapSandboxBackend`` re-exports the same identity as
    ``omnigent.inner.bwrap_sandbox``.

    ``isinstance`` checks against the wrapper class must succeed for
    the registration side effect to be observable through the
    wrapper.
    """
    assert bwrap.BwrapSandboxBackend is inner_bwrap.BwrapSandboxBackend


def test_bwrap_import_triggers_backend_registration() -> None:
    """
    Importing the wrapper module is enough to make the
    ``linux_bwrap`` backend resolvable via ``_get_backend``.

    The contract says importing the wrapper should be functionally
    equivalent to importing ``inner.bwrap_sandbox`` for the purpose of
    side-effecting backend registration. A consumer that switches its
    import path to the wrapper must not lose backend availability.
    """
    backend = inner_sandbox._get_backend("linux_bwrap")
    assert isinstance(backend, bwrap.BwrapSandboxBackend)


def test_default_sandbox_for_platform_is_bwrap_on_linux_regardless_of_binary() -> None:
    """
    The Linux platform default is ``linux_bwrap`` and the choice does
    **not** depend on whether the ``bwrap`` binary is installed on the
    host doing the resolving.

    This is the property that keeps spec parsing host-independent: the
    default is materialized at parse time (loader / spec parser), so a
    CI node or dev laptop without bubblewrap must still be able to load
    a YAML whose ``sandbox:`` block omits ``type:``. The fail-loud
    "no usable mechanism" check is deferred to the backend's runtime
    ``resolve()`` (covered by
    ``tests/inner/test_bwrap_sandbox.py::test_resolve_raises_when_bwrap_missing``).
    We patch ``shutil.which`` to ``None`` to prove a missing binary
    does not change the selected type.
    """
    import sys
    from unittest.mock import patch

    from omnigent.inner.datamodel import OSEnvSandboxSpec

    with (
        patch.object(sys, "platform", "linux"),
        patch("omnigent.inner.sandbox.shutil.which", return_value=None),
    ):
        spec = inner_sandbox._default_sandbox_for_platform()
    assert isinstance(spec, OSEnvSandboxSpec)
    assert spec.type == "linux_bwrap", (
        "Linux default sandbox should be 'linux_bwrap' independent of "
        f"bwrap availability; got {spec.type!r}."
    )


def test_default_sandbox_for_platform_is_seatbelt_on_macos_regardless_of_binary() -> None:
    """
    The macOS platform default is ``darwin_seatbelt`` and, like the
    Linux branch, does not probe for ``sandbox-exec`` — selection is a
    pure platform decision so parsing never depends on the resolving
    host's PATH.
    """
    import sys
    from unittest.mock import patch

    from omnigent.inner.datamodel import OSEnvSandboxSpec

    with (
        patch.object(sys, "platform", "darwin"),
        patch("omnigent.inner.sandbox.shutil.which", return_value=None),
    ):
        spec = inner_sandbox._default_sandbox_for_platform()
    assert isinstance(spec, OSEnvSandboxSpec)
    assert spec.type == "darwin_seatbelt", (
        "macOS default sandbox should be 'darwin_seatbelt' independent of "
        f"sandbox-exec availability; got {spec.type!r}."
    )


def test_default_sandbox_for_platform_raises_on_unsupported_platform() -> None:
    """
    A platform with no sandbox backend at all (anything other than
    Linux or macOS, e.g. Windows) fails loud: there is no backend to
    defer the runtime check to, so the default selector itself raises.
    The operator's explicit opt-out remains
    ``os_env.sandbox.type='none'``.
    """
    import sys
    from unittest.mock import patch

    import pytest

    with (
        patch.object(sys, "platform", "win32"),
        pytest.raises(OSError, match="No sandbox backend is available"),
    ):
        inner_sandbox._default_sandbox_for_platform()
