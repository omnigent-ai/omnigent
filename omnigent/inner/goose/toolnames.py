"""Goose's tool-name dialect.

Goose registers its builtin tools under namespaced ids (``developer__shell``,
``developer__write``), but a ``session/request_permission`` reports ACP's portable
``toolCall.title`` as a friendly label — ``"shell"``, or ``"shell · echo hi"`` with
the command appended. The real id rides in vendor ``_meta``, verified against a
live ``goose acp`` turn (Goose 1.38)::

    toolCall._meta["goose"]["toolCall"]["toolName"] = "developer__shell"

This matters for enforcement, not cosmetics: Omnigent's TOOL_CALL policies match
on the tool name, and the shipped ``ask_on_os_tools`` gates Goose through exactly
``developer__shell`` / ``developer__write`` / ``developer__edit``. Given only the
label, that rule matches nothing and Goose's shell and file writes reach the
model ungated. Reading ``_meta`` here is what makes the rule fire.

Confining it to this module is what keeps the coupling out of the generic ACP
executor, which reads only ACP-standard fields plus whatever a dialect hands it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Goose conveys its real tool id only through this vendor ``_meta`` path, keyed
# relative to the ``toolCall`` object.
_GOOSE_META_KEY = "goose"
_TOOL_CALL_KEY = "toolCall"
_TOOL_NAME_KEY = "toolName"


class GooseToolNameSource:
    """Reads ``_meta.goose.toolCall.toolName`` from a Goose ``toolCall``.

    Fires only when that path is present and holds a non-empty string, so it
    stays inert for any frame that does not carry Goose's dialect — the
    self-gating the :class:`~omnigent.inner.acp_toolnames.AcpToolNameSource`
    protocol requires.
    """

    def read(self, tool_call: Mapping[str, Any]) -> str | None:
        """Return Goose's own tool id, or ``None`` for a non-Goose frame.

        :param tool_call: The ACP ``params.toolCall`` object.
        :returns: e.g. ``"developer__shell"``, else ``None``.
        """
        meta = tool_call.get("_meta")
        if not isinstance(meta, Mapping):
            return None
        vendor = meta.get(_GOOSE_META_KEY)
        if not isinstance(vendor, Mapping):
            return None
        inner = vendor.get(_TOOL_CALL_KEY)
        if not isinstance(inner, Mapping):
            return None
        name = inner.get(_TOOL_NAME_KEY)
        return name if isinstance(name, str) and name else None
