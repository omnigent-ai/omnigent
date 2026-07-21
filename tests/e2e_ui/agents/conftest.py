"""Fixtures for the Agents-rail / sub-agent e2e UI journeys.

Reuses the session-scoped ``live_server`` (and its runner) from the
parent ``tests/e2e_ui/conftest.py``; this module only adds the
sub-agent spec fixtures the ``agents/`` tests need.

The parent agent here (a "joke director") is forbidden from telling
jokes itself: each joke lives ONLY in one of its two inline
``type: agent`` comedian sub-agents, tagged with a per-run nonce. A
joke's nonce reaching the parent's bubbles can therefore only have
traveled through a real ``sys_session_send`` round trip (dispatch,
sub-agent turn, inbox auto-wake) — never from the parent's own world
knowledge. Two distinct comedians means two distinct child sessions,
which is what the Agents-rail tests assert on.
"""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest

# Private helpers from the parent conftest — same import pattern the
# sibling chat tests use for ``open_right_rail`` / ``TwoAgentChatSession``.
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

_JOKE_DIRECTOR_NAME = "joke_director"


@dataclass(frozen=True)
class JokeSubagentsSession:
    """Handle for the two-comedian "joke director" session fixture.

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: The runner-bound parent session id, e.g. ``"conv_abc123"``.
    :param code_one: Per-run nonce only ``comic_one``'s joke carries,
        e.g. ``"scarecrow-3a7f9c2e1b"``.
    :param code_two: Per-run nonce only ``comic_two``'s joke carries,
        e.g. ``"sleepmode-9c2e1b3a7f"``.
    :param routing_token: Per-run token that selects the parent's mock queue.
    """

    base_url: str
    session_id: str
    code_one: str
    code_two: str
    routing_token: str


def _joke_director_yaml(code_one: str, code_two: str) -> str:
    """Build the joke-director spec (parent + two comedian sub-agents).

    Mirrors the omnigent-flavored inline ``type: agent`` shape parsed by
    ``omnigent/inner/loader.py:_parse_tool`` (same as the Hitchhiker's
    fixture in the parent conftest). The parent must dispatch to BOTH
    comedians and relay their jokes verbatim; each joke's nonce appears
    only in that comedian's prompt, so a nonce in the parent's reply
    proves a real two-agent round trip rather than a model-invented joke.

    :param code_one: Per-run nonce in ``comic_one``'s canned joke and nowhere else.
    :param code_two: Per-run nonce in ``comic_two``'s canned joke and nowhere else.
    :returns: YAML text ready for bundle upload.
    """
    return f"""\
name: {_JOKE_DIRECTOR_NAME}
prompt: |
  You are a joke director coordinating two stand-up comedian sub-agents:
  `comic_one` and `comic_two`. You are NOT funny and you must NEVER write
  or guess a joke yourself — only your comedians tell jokes.

  When the user asks you to get some jokes, you MUST do exactly this:

  1. Call `sys_session_send` to ask your `comic_one` sub-agent to tell a joke.
  2. Call `sys_session_send` to ask your `comic_two` sub-agent to tell a joke.

  Then end your turn and wait; do not poll. When the comedians' replies
  arrive in your inbox, relay BOTH jokes to the user VERBATIM — repeat
  every word and every code exactly as written, without omitting or
  altering anything.

  You have exactly ONE of each comedian. If a comedian sub-agent already
  exists, send any follow-up to that SAME sub-agent session — NEVER spawn
  a second `comic_one` or `comic_two`.

executor:
  model: gpt-4o-mini
  harness: openai-agents

tools:
  comic_one:
    type: agent
    description: First stand-up comedian. Tells exactly one joke when asked.
    executor:
      model: gpt-4o-mini
      harness: openai-agents
    prompt: |
      You are a stand-up comedian. When asked for a joke, reply with
      exactly this and nothing else:

      Why did the scarecrow win an award? Because he was outstanding in
      his field. Joke code: {code_one}.
  comic_two:
    type: agent
    description: Second stand-up comedian. Tells exactly one joke when asked.
    executor:
      model: gpt-4o-mini
      harness: openai-agents
    prompt: |
      You are a stand-up comedian. When asked for a joke, reply with
      exactly this and nothing else:

      I told my computer I needed a break, and now it will not stop
      sending me KitKats. Joke code: {code_two}.
"""


@pytest.fixture
def joke_subagents_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[JokeSubagentsSession]:
    """Create a runner-bound session for the two-comedian joke director.

    Same runner-respawn + bind contract as ``two_agent_chat_session`` in
    the parent conftest. Separate content-routed mock queues drive the
    parent and each comedian so concurrent child turns cannot race for a
    shared response. The original model ids remain in the agent spec, so
    a future real-gateway job can reuse the same journey.

    :param live_server: Spawned server fixture from the parent conftest.
    :param mock_llm_server_url: Mock LLM server used by credential-free runs.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: A :class:`JokeSubagentsSession` handle.
    """
    code_one = f"scarecrow-{uuid.uuid4().hex[:10]}"
    code_two = f"kitkat-{uuid.uuid4().hex[:10]}"
    suffix = uuid.uuid4().hex[:10]
    routing_token = f"joke-parent-{suffix}"
    comic_one_token = f"joke-comic-one-{suffix}"
    comic_two_token = f"joke-comic-two-{suffix}"
    yaml_text = _joke_director_yaml(code_one, code_two)

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_comic_one",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "comic_one",
                                "title": "comic_one",
                                "args": f"Tell a joke. Routing marker: {comic_one_token}",
                            }
                        ),
                    },
                    {
                        "call_id": "call_comic_two",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "comic_two",
                                "title": "comic_two",
                                "args": f"Tell a joke. Routing marker: {comic_two_token}",
                            }
                        ),
                    },
                ]
            },
            {"text": "Dispatched both comedians; waiting for their replies."},
            {"text": f"The comedians replied with joke codes {code_one} and {code_two}."},
        ],
        key=routing_token,
        match=routing_token,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Scarecrow joke. Joke code: {code_one}."}],
        key=comic_one_token,
        match=comic_one_token,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Computer joke. Joke code: {code_two}."}],
        key=comic_two_token,
        match=comic_two_token,
    )
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    yaml_bytes = yaml_text.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Non-config.yaml arcname routes the bundle through the omnigent
        # compat adapter, whose loader parses the inline `type: agent`
        # tools. The spec_version:1 parser does not accept this shorthand.
        info = tarfile.TarInfo(name=f"{_JOKE_DIRECTOR_NAME}.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
        yield JokeSubagentsSession(
            base_url=live_server,
            session_id=session_id,
            code_one=code_one,
            code_two=code_two,
            routing_token=routing_token,
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)
