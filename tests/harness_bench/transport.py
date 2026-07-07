"""Transport drivers: the probe-facing contract and the registry.

A probe measures one capability dimension by calling a small set of
*semantic* methods on a driver — ``run_basic_turn``, ``run_streaming_turn``,
``run_tool_turn``, ``run_interrupt_turn`` — each returning a
:class:`~tests.harness_bench.driver.TurnResult`. The driver owns the
*mechanism* (how a tool call is provoked, how a deny is enforced, how deltas
are observed); the probe owns the *interpretation* (what verdict the result
implies). This split is what lets one probe run over transports that reach
the same capability by different means:

- ``sdk-inproc`` (:class:`~tests.harness_bench.driver.SdkInprocDriver`)
  drives a harness wrap subprocess directly, with request-level tools and
  verdict-posted policy.
- ``full-server``
  (:class:`~tests.harness_bench.full_server_driver.FullServerDriver`) drives
  a real server+runner, with a builtin tool and a spec-baked policy.

A kwargs-carrying ``run_turn`` could not bridge these: e.g. streaming is only
observable on full-server via a separate SSE subscription, so "basic turn"
and "streaming turn" must be *distinct* calls, not one call with a flag.

Transport selection (see :func:`resolve_driver_class`). A profile's
``transport`` field is the harness *family* marker, not the literal driver:

- **SDK-family** harnesses (``sdk-inproc``/``full-server``) default to
  ``full-server`` — the fullest coverage, the only transport that exercises
  Tool calling + Policy DENY, and a strict superset of what ``sdk-inproc``
  observes. ``--fast`` downgrades them to ``sdk-inproc``, trading that
  coverage for skipping the server boot.
- **native** harnesses (``native-tui``) have exactly one transport; ``--fast``
  does not apply to them.

A ``--transport`` override wins over both, for any family.
"""

from __future__ import annotations

from typing import Protocol

from tests.harness_bench.driver import TurnResult
from tests.harness_bench.profile import BenchProfile


class Driver(Protocol):
    """The probe-facing driver contract.

    Implementations are async context managers: ``__aenter__`` provisions the
    transport (spawns a wrap subprocess, or a server+runner) and binds a
    session; ``__aexit__`` tears it down. Each ``run_*`` method drives one
    turn and returns a :class:`TurnResult` the probes interpret.

    Not ``@runtime_checkable`` on purpose: drivers are selected by class from
    :func:`driver_registry`, never by ``isinstance`` — and a runtime protocol
    check would not cover the data/static members (``transport``,
    ``unavailable``) anyway. The docstring-only method bodies below are the
    Protocol stub form; the concrete drivers supply the behavior.
    """

    transport: str

    async def __aenter__(self) -> Driver:
        """Provision the transport and bind a session."""

    async def __aexit__(self, *exc: object) -> None:
        """Tear down the transport."""

    async def run_basic_turn(self, marker: str) -> TurnResult:
        """Plain turn asking the model to echo *marker*. Used by basic_turn
        and model_override."""

    async def run_streaming_turn(self) -> TurnResult:
        """A multi-token turn; the result's ``text_delta_count`` reflects
        whether the transport streamed token-level deltas."""

    async def run_tool_turn(self, *, deny: bool) -> TurnResult:
        """Provoke a tool call. With *deny*, a tool-call policy DENY is in
        force so the call should be blocked (``tool_call_denied``); otherwise
        the call is dispatched and answered (``tool_calls`` populated)."""

    async def run_interrupt_turn(self) -> TurnResult:
        """Start a long turn and interrupt it mid-flight; ``cancelled``
        reflects whether the transport honored the interrupt."""

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        """Return a skip reason if this driver cannot run *profile*, else None."""


def driver_registry() -> dict[str, type]:
    """Map transport name → driver class.

    Imported lazily so the transport module stays cheap to import (the
    full-server driver pulls in server/runner spawn helpers).
    """
    from tests.harness_bench.driver import SdkInprocDriver
    from tests.harness_bench.full_server_driver import FullServerDriver
    from tests.harness_bench.native_tui_driver import NativeTuiDriver

    return {
        SdkInprocDriver.transport: SdkInprocDriver,
        FullServerDriver.transport: FullServerDriver,
        NativeTuiDriver.transport: NativeTuiDriver,
    }


# The SDK harness family: transports that drive an SDK-wrap harness. They
# observe the same core dimensions; full-server additionally reaches Tool
# calling + Policy DENY (server-dispatched) and is a strict coverage superset,
# so it is the default. --fast picks the cheaper sdk-inproc within this family.
_SDK_FAMILY = frozenset({"sdk-inproc", "full-server"})
_SDK_DEFAULT = "full-server"
_SDK_FAST = "sdk-inproc"


def resolve_transport_name(profile: BenchProfile, *, override: str | None, fast: bool) -> str:
    """Resolve the effective transport *name* for *profile* from family + flags.

    Precedence: an explicit ``--transport`` *override* wins over everything.
    Otherwise the profile's ``transport`` names a family: an SDK-family harness
    resolves to ``full-server`` (default, fullest coverage) or ``sdk-inproc``
    (under *fast*); a native harness has a single transport that ``--fast``
    does not touch.

    :param profile: The harness under test.
    :param override: ``--transport`` value, or ``None``.
    :param fast: The ``--fast`` flag — downgrade the SDK family to sdk-inproc.
    :returns: The resolved transport name (a key into :func:`driver_registry`).
    """
    if override is not None:
        return override
    if profile.transport in _SDK_FAMILY:
        return _SDK_FAST if fast else _SDK_DEFAULT
    return profile.transport


def resolve_driver_class(
    profile: BenchProfile, *, override: str | None = None, fast: bool = False
) -> type:
    """Resolve the driver *class* for *profile* (see :func:`resolve_transport_name`).

    Raises :class:`KeyError` for an unknown transport so a typo fails loud
    rather than silently falling back.
    """
    name = resolve_transport_name(profile, override=override, fast=fast)
    registry = driver_registry()
    if name not in registry:
        raise KeyError(
            f"unknown transport {name!r}; known transports: {', '.join(sorted(registry))}"
        )
    return registry[name]


__all__ = ["Driver", "driver_registry", "resolve_driver_class", "resolve_transport_name"]
