from __future__ import annotations

import os
from urllib.parse import urlparse

from omnigent.spec.types import AgentSpec, MCPServerConfig

COMPANY_BRAIN_MCP_NAME = "company-brain"
COMPANY_BRAIN_MCP_TOKEN_HEADER = "X-Omnigent-Company-Brain-Token"
COMPANY_BRAIN_MCP_URL_HEADER = "X-Omnigent-Company-Brain-Url"
COMPANY_BRAIN_TOOLS = [
    "context_pack",
    "delta",
    "get_page",
    "recall",
    "search",
    "synthesize",
    "traverse_graph",
]


def attach_company_brain_mcp(spec: AgentSpec, *, url: str, token: str) -> AgentSpec:
    for child in spec.sub_agents:
        if child.company_brain:
            attach_company_brain_mcp(child, url=url, token=token)
    if not spec.company_brain:
        return spec
    if any(server.name == COMPANY_BRAIN_MCP_NAME for server in spec.mcp_servers):
        return spec
    if not url or not token:
        raise ValueError("company_brain is enabled but its managed MCP URL or token is missing")
    parsed = urlparse(url)
    loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not loopback:
        raise ValueError("company brain MCP URL must use HTTPS or loopback HTTP")
    if not parsed.netloc:
        raise ValueError("company brain MCP URL must be absolute")
    spec.mcp_servers.append(
        MCPServerConfig(
            name=COMPANY_BRAIN_MCP_NAME,
            transport="http",
            url=url,
            headers={"Authorization": f"Bearer {token}"},
            tools=list(COMPANY_BRAIN_TOOLS),
            description="Company-shared knowledge with cited retrieval and synthesis.",
            timeout=120,
        )
    )
    return spec


def resolve_company_brain_mcp(spec: AgentSpec) -> AgentSpec:
    url = os.environ.get("OMNIGENT_COMPANY_BRAIN_MCP_URL", "").strip()
    token = os.environ.get("OMNIGENT_COMPANY_BRAIN_MCP_TOKEN", "").strip()
    return attach_company_brain_mcp(spec, url=url, token=token)
