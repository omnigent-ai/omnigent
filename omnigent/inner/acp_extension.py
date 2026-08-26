"""Vendor extension seam for the generic ACP executor.

The Agent Client Protocol leaves the interesting capabilities unspecified —
sub-agents, permission-option semantics, hook surfaces — so agents fill those
gaps in their own dialects. :class:`AcpExtension` is the single seam where such
vendor behavior attaches to :class:`~omnigent.inner.acp_executor.AcpExecutor`,
which keeps the executor protocol-pure: it reads only ACP-standard fields plus
whatever an extension hands it.

An extension is **composed at the harness boundary, not discovered**. A vendor's
harness wrap passes its extension to the shared ACP wrap
(:func:`omnigent.inner.acp_harness.create_app`); the generic ``acp`` harness and
every other builtin ACP row run with :data:`NO_ACP_EXTENSION` and behave exactly
as they did before any extension existed. There is no registry to consult and no
harness-name branch anywhere in the generic path.

That composition is what keeps a vendor liftable. Everything vendor-specific is
one package plus one ``create_app()`` — the same shape as a community harness
plugin under ``omnigent.community.harness.<name>`` — so moving a vendor out of
core is a directory move plus an entry point, not a rewrite. See
:mod:`omnigent.inner.devin` for the worked example.

**Adding a capability is additive**: give :class:`AcpExtension` a field with a
neutral default, read it at the one place in the executor where it applies, and
populate it from the vendor's extension. Everything that doesn't set it is
unaffected.

The live candidate is ``initialize``-time client capabilities. Devin reports
sub-agents unconditionally, but Claude Code's ACP bridge gates its nested
transcript on the client opting in with
``clientCapabilities._meta["subagent-transcript"] = true`` and then relates
nested updates via ``_meta.claudeCode.parentToolUseId``. Supporting that dialect
therefore needs an extension to contribute handshake fields, not just read
frames — an ``initialize`` capability contribution alongside
:attr:`AcpExtension.subagent_sources`. Others: filtering which permission
options reach the approval card (an agent may offer "switch to bypass mode"),
resolving a vendor ``_meta`` tool name when the protocol's ``title`` is absent,
and vendor-specific tool-gating hooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from omnigent.inner.acp_subagents import AcpSubAgentSource


@dataclass(frozen=True)
class AcpExtension:
    """Vendor behavior layered onto the generic ACP executor.

    :param name: The vendor this extension speaks for, e.g. ``"devin"``. Used in
        logs and to identify the extension in tests; never matched against a
        harness id to select behavior (the harness wrap does the selecting).
    :param subagent_sources: Dialects that recognize this vendor's sub-agent
        lifecycle reporting (see :mod:`omnigent.inner.acp_subagents`). Empty —
        the default — means the executor does no sub-agent scanning at all.
    """

    name: str
    subagent_sources: tuple[AcpSubAgentSource, ...] = field(default=())

    @property
    def surfaces_subagents(self) -> bool:
        """Whether this vendor's sub-agents become Omnigent child sessions.

        The source of truth for the harness's declared ``subagents`` capability,
        so the capability matrix cannot drift from what the code actually does.
        """
        return bool(self.subagent_sources)


# The generic ACP harness and every builtin ACP CLI row that declares no vendor
# behavior. Reads as "protocol only".
NO_ACP_EXTENSION = AcpExtension(name="acp")
