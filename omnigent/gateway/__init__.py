"""Host-level gateway servlet for gateway-backed harness traffic.

Self-contained within Omnigent (stdlib + httpx + starlette + uvicorn only)
so the component can later move out of the host process wholesale: the host
starts it and everything else reaches it through :class:`GatewayHandle` or
its loopback HTTP admin API.
"""

from omnigent.gateway.servlet import GatewayHandle, GatewayServlet, start_gateway_servlet
from omnigent.gateway.state import (
    ServletState,
    clear_servlet_state,
    read_servlet_state,
    write_servlet_state,
)

__all__ = [
    "GatewayHandle",
    "GatewayServlet",
    "ServletState",
    "clear_servlet_state",
    "read_servlet_state",
    "start_gateway_servlet",
    "write_servlet_state",
]
