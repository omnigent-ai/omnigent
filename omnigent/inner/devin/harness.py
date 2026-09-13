"""``harness: devin`` wrap.

The entry point :mod:`omnigent.runtime.harnesses._runner` invokes after the
plugin registry resolves ``"devin"`` to this module. Devin speaks ACP, so this
adds no protocol code: it delegates to the shared ACP wrap and injects
:data:`~omnigent.inner.devin.DEVIN_ACP_EXTENSION`, which is the only thing that
distinguishes a Devin harness process from a generic ``acp`` one.

Env contract is the generic ACP one (``HARNESS_ACP_*``, see
:mod:`omnigent.inner.acp_harness`) because Devin is a builtin ACP CLI row whose
command and argv come from :data:`omnigent.acp_cli_harnesses.ACP_CLI_HARNESSES`.
A Devin-specific variable would be declared here and read alongside it.
"""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner import acp_harness
from omnigent.inner.devin import DEVIN_ACP_EXTENSION


def create_app() -> FastAPI:
    """Build the Devin harness's FastAPI app (required entry point).

    :returns: The app the runner serves for a ``harness: devin`` session.
    """
    return acp_harness.create_app(extension=DEVIN_ACP_EXTENSION)
