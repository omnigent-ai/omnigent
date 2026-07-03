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

Transport selection: each :class:`BenchProfile` declares a default
``transport``; a ``--transport`` CLI override wins over it globally (see
:func:`resolve_driver_class`).
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

    return {
        SdkInprocDriver.transport: SdkInprocDriver,
        FullServerDriver.transport: FullServerDriver,
    }


def resolve_driver_class(profile: BenchProfile, *, override: str | None) -> type:
    """Resolve the driver class for *profile*.

    *override* (the ``--transport`` flag) wins over the profile's declared
    ``transport`` when set. Raises :class:`KeyError` for an unknown transport
    so a typo fails loud rather than silently falling back.

    :param profile: The harness under test.
    :param override: A transport name from ``--transport``, or ``None`` to use
        the profile's declared transport.
    :returns: The driver class to instantiate.
    """
    name = override or profile.transport
    registry = driver_registry()
    if name not in registry:
        raise KeyError(
            f"unknown transport {name!r}; known transports: {', '.join(sorted(registry))}"
        )
    return registry[name]


__all__ = ["Driver", "driver_registry", "resolve_driver_class"]
