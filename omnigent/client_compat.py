"""Detect a stale ``omnigent-client`` install and explain the fix.

``omnigent`` and ``omnigent-client`` ship as a version-locked pair (each
pins ``==`` the other in ``pyproject.toml``). When the installed client
lags the running ``omnigent`` code — e.g. a build refreshed one package but
not its sibling, or a checkout runs against an older wheel — importing a
symbol the newer server added fails deep in a lazy import:

    ImportError: cannot import name 'RegisteredAgent' from 'omnigent_client'

Without help that surfaces as the generic crash screen + bug-filing prompt,
which sends users chasing a phantom bug. This module recognises the skew and
turns it into a one-line "your client is out of date, run this" message.
"""

from __future__ import annotations

import importlib.metadata as _metadata

from omnigent.version import VERSION

# The sibling distribution that pairs with the running ``omnigent`` code.
_CLIENT_DIST = "omnigent-client"
# Import name of the same package (``exc.name`` on the failed import).
_CLIENT_MODULE = "omnigent_client"


def installed_client_version() -> str | None:
    """Return the installed ``omnigent-client`` version, or ``None``.

    ``None`` means the distribution metadata is absent (not installed, or a
    source tree not on the metadata path) — we still report the skew, just
    without a concrete version to name.
    """
    try:
        return _metadata.version(_CLIENT_DIST)
    except _metadata.PackageNotFoundError:
        return None


def is_client_skew_error(exc: BaseException) -> bool:
    """True if *exc* (or its cause/context chain) is a failed
    ``omnigent_client`` import — a missing module or a missing symbol.

    Both :class:`ModuleNotFoundError` ("No module named 'omnigent_client'")
    and the "cannot import name X from 'omnigent_client'" form set
    ``exc.name`` to the module, so that single attribute is the signal.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ImportError) and getattr(current, "name", None) == _CLIENT_MODULE:
            return True
        current = current.__cause__ or current.__context__
    return False


def client_skew_message(exc: BaseException) -> str | None:
    """Actionable guidance for a client-skew *exc*, or ``None`` if *exc*
    is not one.

    The message names the running ``omnigent`` version and the stale client
    version (when resolvable) and points at both the installed-wheel and
    dev-clone update paths, so it is correct regardless of how the user
    installed omnigent.
    """
    if not is_client_skew_error(exc):
        return None

    client = installed_client_version()
    if client is not None:
        mismatch = f"omnigent-client {client} is out of sync with omnigent {VERSION}"
    else:
        mismatch = f"omnigent-client is not installed for omnigent {VERSION}"

    return (
        f"{mismatch}.\n"
        "They ship as a version-locked pair; update your install so they "
        "match:\n"
        "  • installed:  omni upgrade\n"
        "  • dev clone:  uv sync   (or: uv pip install -e sdks/python-client)\n"
        f"\nUnderlying import error: {exc}"
    )
