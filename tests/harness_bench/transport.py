"""Transport-independent driver protocol and driver resolution."""

from __future__ import annotations

from typing import Protocol

from tests.harness_bench.driver import ForkResult, TurnResult
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

    async def run_mcp_tool_turn(self) -> TurnResult:
        """Provoke an Omnigent MCP relay tool call.

        Native transports use this to distinguish the Omnigent MCP bridge from
        the vendor's own shell/tool surface. Unsupported transports return an
        unmeasured result so the probe SKIPs.
        """

    async def run_fork_turn(self, marker: str) -> ForkResult:
        """Fork the current session and prove its history is replayed."""

    async def run_policy_turn(self, *, action: str) -> TurnResult:
        """Provoke a tool call under an explicit tool-call policy *action*
        (``"allow"`` or ``"ask"``), for the policy_allow / policy_ask probes.

        - ``"allow"``: an explicit ALLOW policy is in force; the call should
          proceed (``tool_call_allowed`` set once dispatched + answered).
        - ``"ask"``: an ASK policy is in force; the call should raise an
          elicitation (``elicitation_requested`` set), which the driver then
          resolves so the turn can settle.

        A transport that cannot surface the requested action returns an
        unmeasured result so the probe SKIPs (never a false verdict)."""

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


# Full-server covers server-dispatched tools; --fast uses cheaper sdk-inproc.
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
