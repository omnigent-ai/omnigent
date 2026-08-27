"""Vendor tool-name resolution for ACP agents.

A ``session/request_permission`` names the tool through ACP's portable
``toolCall.title`` (plus ``kind``), which is a *human label* — an agent is free to
send ``"shell"``, or ``"shell · echo hi"``, for a tool its own runtime registers
under some other name. Omnigent's TOOL_CALL policies match on the name, so a
label the agent never dispatches by is a name no rule can gate.

Agents that expose their real tool identifier put it in a vendor ``_meta`` block,
in their own dialect and under their own key. This module owns only the
**generic** half: the :class:`AcpToolNameSource` protocol mapping one dialect
onto a name, and :func:`read_tool_name` to run them. It reads no vendor field and
names no vendor; :class:`~omnigent.inner.acp_executor.AcpExecutor` runs whatever
sources its :class:`~omnigent.inner.acp_extension.AcpExtension` supplies and
falls back to the portable fields when none matches.

A vendor's dialect lives with that vendor — see
:class:`omnigent.inner.goose.toolnames.GooseToolNameSource` for the worked
example — so supporting another agent means adding one source in that agent's own
package, with nothing here or downstream to change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AcpToolNameSource(Protocol):
    """Maps one agent's tool-name dialect onto the name policy should match.

    Pure and stateless: given a single ACP ``toolCall`` object, return the
    vendor's own tool identifier, or ``None`` when this dialect does not
    recognize the frame.

    An implementation MUST self-gate on its own dialect's markers rather than
    assume it is only handed its own vendor's traffic, for the same reasons as
    :class:`~omnigent.inner.acp_subagents.AcpSubAgentSource`: a source is the only
    thing that knows its dialect, and self-gating keeps it correct if an agent's
    fork or a future multi-dialect wrap hands it frames it does not recognize.
    """

    def read(self, tool_call: Mapping[str, Any]) -> str | None: ...


def read_tool_name(
    tool_call: Mapping[str, Any],
    sources: Sequence[AcpToolNameSource],
) -> str | None:
    """Return the first vendor tool name a source recognizes, else ``None``.

    :param tool_call: The ACP ``params.toolCall`` object.
    :param sources: Dialects to try, from the executor's
        :class:`~omnigent.inner.acp_extension.AcpExtension`. Empty (the generic
        ACP harness) short-circuits to ``None``, leaving the portable
        ``title`` / ``kind`` fields authoritative.
    :returns: The vendor's tool identifier, or ``None`` when no source matched.
    """
    for source in sources:
        name = source.read(tool_call)
        if name:
            return name
    return None
