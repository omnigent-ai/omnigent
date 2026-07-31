"""Subprocess harness module for echo-native (``harness_modules`` target).

``omnigent.runtime.harnesses`` maps ``echo-native`` → this module; the runner
imports it and calls ``create_app()`` to serve the harness over a Unix socket.
A real native harness returns a FastAPI app that bridges the vendor process
(see ``omnigent.inner.pi_native_harness``). The example keeps a minimal factory
so the module contract is legible.
"""

from __future__ import annotations

from typing import Any


def create_app() -> Any:
    """Return the harness ASGI app the runner serves (``create_app`` contract).

    :returns: A FastAPI (ASGI) application.
    """
    # TODO(real-harness): build and return the harness FastAPI app that proxies
    # the vendor process. See omnigent.inner.pi_native_harness.create_app.
    raise NotImplementedError(
        "echo-native is an example harness stub; create_app has no live harness app. "
        "See omnigent.inner.pi_native_harness for a real implementation."
    )
