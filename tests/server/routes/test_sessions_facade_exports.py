"""Guards the ``sessions`` facade re-exports the ``_sessions`` impl package.

The facade (``omnigent.server.routes.sessions``) is assembled by star-importing
the ``_sessions`` impl modules, each of which pins an explicit ``__all__``. A
name defined in one impl module but omitted from its ``__all__`` is invisible to
a star-importing sibling — so a cross-module call resolves to ``NameError`` only
at runtime, on the branch that reaches it. These tests pin the seams that have
bitten us so the omission fails at import/collection time instead.
"""

from __future__ import annotations

import pytest


def test_harness_override_executor_type_reexported() -> None:
    """The ``harness_override == "auto"`` gate crosses a module boundary.

    ``_validated_harness_override_executor_type`` lives in ``helpers`` but is
    called from ``orchestration`` (via star-import) on the auto path. If it is
    dropped from ``helpers.__all__`` the call is a ``NameError`` at session
    creation — assert it is reachable through both the facade and the calling
    module's namespace.
    """
    from omnigent.server.routes import sessions as facade
    from omnigent.server.routes._sessions import orchestration

    assert callable(facade._validated_harness_override_executor_type)
    assert callable(orchestration._validated_harness_override_executor_type)


@pytest.mark.parametrize(
    "name",
    [
        "_HOST_RUNNER_STATUS_TIMEOUT_S",
        "_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S",
    ],
)
def test_timeout_constants_reexported(name: str) -> None:
    """Timeout constants must live on the facade — it is the monkeypatch target.

    Impl modules read these through the facade so a facade-level patch is
    honored; that only works if the facade actually re-exports them.
    """
    from omnigent.server.routes import sessions as facade

    assert isinstance(getattr(facade, name), float)
