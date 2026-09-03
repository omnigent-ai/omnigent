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

The tell is a failed ``omnigent_client`` import *plus* a version that differs
from the running code (``omnigent.version.VERSION``). Requiring the mismatch
matters: a genuine missing-symbol bug committed at the *same* version is a
real bug, not a skew, and must still reach the crash handler with its
traceback rather than be papered over with "run an upgrade".
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
    bare source tree on ``sys.path`` with no dist-info) — a state that is
    itself a skew symptom, so callers treat it as one.
    """
    try:
        return _metadata.version(_CLIENT_DIST)
    except _metadata.PackageNotFoundError:
        return None


def _failed_client_import(exc: BaseException) -> bool:
    """True if *exc* (or its cause/context chain) is a failed
    ``omnigent_client`` import — a missing module or a missing symbol.

    Both ``ModuleNotFoundError`` ("No module named 'omnigent_client'") and the
    "cannot import name X from 'omnigent_client'" form set ``exc.name`` to the
    module, so that attribute is the signal. A dotted submodule miss (e.g.
    ``omnigent_client.foo``) is matched by prefix.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ImportError):
            name = getattr(current, "name", None)
            if name == _CLIENT_MODULE or (
                name is not None and name.startswith(f"{_CLIENT_MODULE}.")
            ):
                return True
        current = current.__cause__ or current.__context__
    return False


def is_client_skew_error(exc: BaseException) -> bool:
    """True if *exc* is a failed ``omnigent_client`` import **and** the
    installed client is a different version than the running code.

    A same-version failure is a genuine bug, not a stale install, and returns
    ``False`` so it reaches the normal crash handler. A missing client
    (metadata absent) cannot be ruled out, so it counts as a skew.
    """
    if not _failed_client_import(exc):
        return False
    client = installed_client_version()
    return client is None or client != VERSION


def client_skew_message(exc: BaseException) -> str | None:
    """Actionable guidance for a client-skew *exc*, or ``None`` when *exc*
    is not one (unrelated error, or a same-version genuine bug).

    Names the running ``omnigent`` version and the installed client version,
    and points at both the installed-wheel and dev-clone update paths, so it
    is correct regardless of how omnigent was installed.
    """
    if not is_client_skew_error(exc):
        return None

    client = installed_client_version()
    if client is not None:
        headline = (
            f"omnigent-client {client} does not match omnigent {VERSION} — "
            "they ship as a version-locked pair."
        )
    else:
        headline = (
            f"omnigent-client is not installed for omnigent {VERSION} — "
            "they ship as a version-locked pair."
        )

    return (
        f"{headline}\n"
        "Update your install so they match:\n"
        "  • installed:  omni upgrade\n"
        "  • dev clone:  uv sync   (or: uv pip install -e sdks/python-client)\n"
        f"\nUnderlying import error: {exc}"
    )
