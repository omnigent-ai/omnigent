"""Runner agent-bundle spec parsing under sub-agent fan-out.

Drives the runner app's real session-creation boundary
(``POST /v1/sessions``) wired to the real
``_resolve_agent_spec_from_server`` resolver, backed by a mock server
serving one multi-agent bundle. Creating a parent session plus N
sub-agent sessions that all share one ``(agent_id, version)`` bundle
must not re-parse the identical YAML once per session, and the spec
YAML loader must be the libyaml-accelerated one when it is available.

Hermetic: no live server, no LLM key, no harness CLI needed.

Run::

    pytest tests/e2e/test_spec_parse_fanout.py -o addopts= -q
"""

from __future__ import annotations

import io
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

import omnigent.spec as spec_pkg
import omnigent.spec.parser as parser_mod
from omnigent.runner import create_runner_app
from omnigent.runner._entry import _resolve_agent_spec_from_server
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


class _FakeProcessManager:
    """Minimal HarnessProcessManager stand-in for session creation."""

    handles_tool_dispatch = True

    def __init__(self) -> None:
        self._sessions: set[str] = set()

    async def get_client(self, conversation_id: str, harness: str, env: Any = None) -> Any:
        self._sessions.add(conversation_id)
        return None

    def has_session(self, conversation_id: str) -> bool:
        return conversation_id in self._sessions

    def has_active_turn(self, conversation_id: str) -> bool:
        return False

    def note_activity(self, conversation_id: str) -> None:
        pass

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        pass

    def clear_in_flight(self, conversation_id: str) -> None:
        pass

    async def forward_cancel(self, conversation_id: str) -> bool:
        return True

    async def release(self, conversation_id: str) -> None:
        self._sessions.discard(conversation_id)


async def test_fanout_does_not_reparse_bundle_per_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One shared bundle must not be YAML-parsed once per created session.

    Journey: launch an orchestrator agent (parent session), then fan out
    N sub-agent sessions of the same agent. All N+1 creates resolve the
    same ``(agent_id, version)`` bundle; the extracted files are already
    disk-cached, so at most the initial resolution should parse the
    top-level config.yaml. Re-parsing per session is the CPU burst users
    observe on the runner during sub-agent fan-out.

    :param monkeypatch: Used to count parses at the parser boundary.
    :returns: None.
    """
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

    spec_cache_root = Path(tempfile.mkdtemp(prefix="fanout-specs-"))
    resolver_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_bundle_handler),
        base_url="http://server.test",
    )

    # Mirror the runner entry point's spec_resolver closure, including the
    # runner-lifetime parsed-spec memo it threads through when the resolver
    # supports one. Introspecting keeps the failure behavioral (a parse
    # count) rather than a TypeError on trees without the memo parameter.
    import inspect

    resolver_state: dict[str, Any] = {}
    if "spec_parse_cache" in inspect.signature(_resolve_agent_spec_from_server).parameters:
        resolver_state["spec_parse_cache"] = {}

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> Any:
        # Mirrors the runner entry point's spec_resolver closure.
        return await _resolve_agent_spec_from_server(
            resolver_client,
            spec_cache_root,
            agent_id,
            session_id=session_id,
            **resolver_state,
        )

    app = create_runner_app(
        process_manager=_FakeProcessManager(),  # type: ignore[arg-type]
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
    # Budget: one parse to validate the freshly extracted bundle plus one
    # for the first in-memory resolution. Anything above that means the
    # identical (agent_id, version) bundle is re-parsed per session.
    assert len(top_level_parses) <= 2, (
        f"Creating 1 parent + {N_SUB_AGENTS} sub-agent sessions sharing one "
        f"(agent_id, version) bundle parsed the top-level config.yaml "
        f"{len(top_level_parses)} times ({len(parse_roots)} YAML parses "
        f"total, sub-agent configs included). The parsed AgentSpec must be "
        f"memoized by (agent_id, version) so fan-out does not burn runner "
        f"CPU re-parsing identical YAML."
    )


def test_config_yaml_loader_uses_libyaml() -> None:
    """Spec parsing must use the libyaml-accelerated safe loader.

    The pure-Python ``yaml.SafeLoader`` is ~10-20x slower than
    ``yaml.CSafeLoader`` on the same config.yaml; multiplied across a
    multi-agent bundle's files and per-session re-resolution it becomes
    a visible fraction of runner CPU during sub-agent fan-out.

    :returns: None.
    """
    if not getattr(yaml, "__with_libyaml__", False):
        pytest.skip("PyYAML built without libyaml; CSafeLoader unavailable")

    from omnigent.spec.parser import _ConfigYamlLoader

    assert issubclass(_ConfigYamlLoader, yaml.CSafeLoader), (
        "_ConfigYamlLoader subclasses the pure-Python yaml.SafeLoader even "
        "though the libyaml CSafeLoader is available in this runtime; spec "
        f"parsing pays a ~10-20x CPU penalty per parse. MRO: "
        f"{[cls.__name__ for cls in _ConfigYamlLoader.__mro__]}"
    )
