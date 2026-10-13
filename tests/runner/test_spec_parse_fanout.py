"""Exercise runner session creation with a mock server supplying a shared bundle.

Parent and child sessions must reuse its parsed spec. These tests use the
real resolver in process, with no live server or model."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.spec as spec_pkg
import omnigent.spec.parser as parser_mod
from omnigent.runner import create_runner_app
from omnigent.runner._entry import _resolve_agent_spec_from_server
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient

N_SUB_AGENTS = 4

_PARENT_CONFIG = """\
spec_version: 1
name: fanout-parent
description: orchestrator that fans out sub-agents
executor:
  type: omnigent
  config:
    harness: claude-sdk
    model: gpt-5.4
instructions: |
  You are an orchestrator. Dispatch work to your sub-agents and collect
  their results. {pad}
tools:
  agents: [{agent_list}]
"""

_SUB_CONFIG = """\
spec_version: 1
name: {name}
description: worker sub-agent {name}
executor:
  type: omnigent
  config:
    harness: claude-sdk
    model: gpt-5.4
instructions: |
  You are worker {name}. Do the assigned work and report back. {pad}
"""


def _build_multi_agent_bundle() -> bytes:
    """Build one parent-with-sub-agents bundle tarball in memory.

    :returns: Gzipped tarball bytes with a top-level ``config.yaml``
        and ``agents/worker-N/config.yaml`` for each worker.
    """
    pad = "x " * 400  # realistic instruction body so the parse has weight
    names = [f"worker-{i}" for i in range(N_SUB_AGENTS)]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:

        def _add(path: str, text: str) -> None:
            data = text.encode()
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        _add(
            "config.yaml",
            _PARENT_CONFIG.format(pad=pad, agent_list=", ".join(names)),
        )
        for name in names:
            _add(
                f"agents/{name}/config.yaml",
                _SUB_CONFIG.format(name=name, pad=pad),
            )
    return buf.getvalue()


async def test_fanout_does_not_reparse_bundle_per_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Session creation must reuse the parse for a shared (agent_id, version) bundle."""
    bundle = _build_multi_agent_bundle()

    def _bundle_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=bundle,
            headers={"X-Agent-Version": "1", "X-Agent-Session-Scoped": "false"},
        )

    parse_roots: list[str] = []
    real_parse = parser_mod.parse

    def _counting_parse(root: Path, **kwargs: Any) -> Any:
        parse_roots.append(str(root))
        return real_parse(root, **kwargs)

    # spec.load resolves ``parse`` through both namespaces; patch both so
    # every top-level bundle parse is observed regardless of entry point.
    monkeypatch.setattr(parser_mod, "parse", _counting_parse)
    monkeypatch.setattr(spec_pkg, "parse", _counting_parse)
    monkeypatch.setattr("omnigent.runner._entry._SPEC_PARSE_CACHE", {})

    spec_cache_root = tmp_path
    resolver_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_bundle_handler),
        base_url="http://server.test",
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> Any:
        # Mirrors the runner entry point's spec_resolver closure.
        return await _resolve_agent_spec_from_server(
            resolver_client,
            spec_cache_root,
            agent_id,
            session_id=session_id,
        )

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            resp = await client.post(
                "/v1/sessions",
                json={"session_id": "conv_parent", "agent_id": "ag_fanout"},
            )
            assert resp.status_code == 201, resp.text
            for i in range(N_SUB_AGENTS):
                resp = await client.post(
                    "/v1/sessions",
                    json={
                        "session_id": f"conv_sub_{i}",
                        "agent_id": "ag_fanout",
                        "sub_agent_name": f"worker-{i}",
                    },
                )
                assert resp.status_code == 201, resp.text
    finally:
        await resolver_client.aclose()

    # Sub-agent discovery parses agents/*/config.yaml as part of a
    # top-level parse; count only the top-level bundle parses.
    top_level_parses = [root for root in parse_roots if "/agents/" not in root]
    # Extraction parses the bundle once; every later session must hit the memo.
    assert len(top_level_parses) == 1, (
        f"Creating 1 parent + {N_SUB_AGENTS} sub-agent sessions sharing one "
        f"bundle parsed the top-level config.yaml {len(top_level_parses)} times "
        f"({len(parse_roots)} YAML parses total, sub-agent configs included). "
        f"The parsed AgentSpec must be memoized per bundle revision so fan-out "
        f"does not burn runner CPU re-parsing identical YAML."
    )
