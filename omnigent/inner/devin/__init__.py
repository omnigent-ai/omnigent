"""Devin harness — ACP-driven, layered on the generic ACP executor.

Devin (Cognition's ``devin`` CLI) runs through Omnigent's generic ACP executor,
so this package holds only what is genuinely Devin's: its sub-agent dialect
(:mod:`.subagents`), the extension that declares it (:data:`DEVIN_ACP_EXTENSION`,
composed below), and the harness wrap that injects it (:mod:`.harness`). Nothing
in the generic ACP path names Devin.

**Composition root.** This module assembles the extension from its parts, so a
new Devin-specific capability is a new sibling module plus one field here — for
example a ``hooks.py`` gating tool calls once ACP grows a hook surface — and the
generic executor learns about it through
:class:`~omnigent.inner.acp_extension.AcpExtension` rather than a Devin import.

**Liftability.** The layout mirrors a community harness plugin
(``omnigent/community/harness/<name>/{plugin.py,inner/}``, per
https://omnigent.ai/docs/build/harnesses/community): a package of vendor code
plus a ``create_app()``. Moving Devin out of core is that move plus an entry
point in ``omnigent.community.harness`` — with two things to know:

* a community plugin may not override a **builtin** harness name, so the move
  means deleting Devin's builtin catalog row in the same change, not adding a
  package alongside it; and
* the plugin contract's public surface is
  :class:`~omnigent.inner.executor.Executor` +
  :class:`~omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`, so an
  out-of-core Devin either imports the ACP executor from core (a private module
  today) or carries its own ACP client, as ``omnigent-rovo`` does. Keeping this
  package to dialect + wrap is what makes that choice cheap either way.
"""

from __future__ import annotations

from omnigent.inner.acp_extension import AcpExtension
from omnigent.inner.devin.subagents import DevinSubAgentSource

#: Everything the generic ACP executor needs to behave as Devin. Injected by
#: :func:`omnigent.inner.devin.harness.create_app`, and the source of truth for
#: Devin's declared ``subagents`` capability in ``harness_plugins``.
DEVIN_ACP_EXTENSION = AcpExtension(
    name="devin",
    subagent_sources=(DevinSubAgentSource(),),
)

__all__ = ["DEVIN_ACP_EXTENSION", "DevinSubAgentSource"]
