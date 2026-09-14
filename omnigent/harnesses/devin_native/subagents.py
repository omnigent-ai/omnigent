"""Reconstruct Devin sub-agent transcripts from the CLI's on-disk session store.

Devin runs each ``run_subagent`` delegate as its own chain inside the *parent*
session's message forest — ``message_nodes`` in
``$XDG_DATA_HOME/devin/cli/sessions.db`` (default ``~/.local/share/...``) — not as
a separate session, and not in the lifecycle-hook stream or the ATIF export.
Reading it back is the direct analogue of how ``claude-native`` mirrors
sub-agents: Claude Code writes one transcript file per Task-tool child under
``~/.claude/projects/<enc>/<session>/subagents/`` and the forwarder tails it;
Devin persists the same thing in SQLite, so we read the same thing from SQLite.

Shape captured from ``devin 3000.10.21`` (a turn spawning two parallel
sub-agents):

* Each sub-agent chain roots at a ``role="system"`` node beginning
  ``"You are a subagent of Devin"``; its first ``role="user"`` child is the
  ``run_subagent`` ``task`` **verbatim**.
* The spawned ``agent_id`` appears only in the ``run_subagent`` *result* text
  (``"...started with agent_id=<id>"``) and in the parent's
  ``<subagent_completion_notification>``; sub-agent nodes carry no id of their
  own. So ``(task -> agent_id)`` comes from the tool stream and keys the chain.
* The forest also stores compaction/streaming snapshots — several dead-end
  copies of the same task root. Walking ``parent_node_id`` *up* from a leaf
  sidesteps that: a leaf has exactly one ancestor path, so per task root the
  **longest** leaf-chain is the executed one.
* Sub-agents spawned with the SAME task text get one chain each, so the chains
  are kept apart (:func:`reconstruct_transcript_chains`) and matched to their
  ``agent_id`` by the final report the parent's
  ``<subagent_completion_notification>`` quotes — the only place the two meet.
  ``subagent_heads`` (the live ``agent_id -> head`` table) is empty once a
  background sub-agent has been collected, so it is not relied on.

The pure functions here (:func:`reconstruct_transcript_nodes`,
:func:`transcript_items`, the parsers) are exercised against a captured fixture
in ``tests/test_devin_native_subagents.py``; :func:`load_message_nodes` is the
only I/O and opens the vendor DB read-only so it never contends with the live
Devin writer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

from omnigent.util.json_types import JsonObject as _JsonObject

#: A sub-agent chain's root system node opens with this sentence.
SUBAGENT_ROOT_PREFIX = "You are a subagent of Devin"
#: The tool the parent calls to spawn / to collect a background sub-agent.
RUN_SUBAGENT_TOOL = "run_subagent"
READ_SUBAGENT_TOOL = "read_subagent"

# The spawned id rides only free text: "...started with agent_id=690d786b. ...".
_SPAWNED_ID_RE = re.compile(r"agent_id=([0-9A-Za-z_-]+)")
# The parent's completion notification names the finished sub-agent.
_COMPLETED_ID_RE = re.compile(r"\[Background subagent with agent_id=([0-9A-Za-z_-]+) completed\]")
#: One completion notification's ``agent_id`` plus the report body after it, up to
#: the next notification or the closing tag. That body is the sub-agent's own
#: final text, so it says which chain belongs to which agent.
_COMPLETION_REPORT_RE = re.compile(
    r"\[Background subagent with agent_id=([0-9A-Za-z_-]+) completed\]\s*(.*?)"
    r"(?=\[Background subagent with agent_id=|</subagent_completion_notification>|\Z)",
    re.DOTALL,
)


def devin_sessions_db_path(source_env: Mapping[str, str]) -> Path | None:
    """Return the path to Devin's session SQLite DB, or ``None`` if unlocatable.

    :param source_env: The environment Devin was launched with (``XDG_DATA_HOME``
        wins; otherwise ``HOME``), so the runner reads the same store the CLI
        wrote.
    :returns: ``<data>/devin/cli/sessions.db``, or ``None`` when neither
        ``XDG_DATA_HOME`` nor ``HOME`` is set.
    """
    xdg = source_env.get("XDG_DATA_HOME")
    if xdg:
        base: Path | None = Path(xdg)
    else:
        home = source_env.get("HOME")
        base = Path(home) / ".local" / "share" if home else None
    if base is None:
        return None
    return base / "devin" / "cli" / "sessions.db"


def parse_spawned_agent_id(output_text: str | None) -> str | None:
    """Pull the ``agent_id`` out of a ``run_subagent`` result.

    :param output_text: The ``run_subagent`` tool result text, e.g.
        ``"Background subagent started with agent_id=690d786b. ..."``.
    :returns: The sub-agent id, or ``None`` when the text carries none.
    """
    match = _SPAWNED_ID_RE.search(output_text or "")
    return match.group(1) if match else None


def completed_agent_ids(text: str | None) -> list[str]:
    """Return the sub-agent ids named as completed in *text*.

    :param text: A ``<subagent_completion_notification>`` body (or any text that
        may embed ``"[Background subagent with agent_id=<id> completed]"``).
    :returns: Every completed sub-agent id found, in order.
    """
    return _COMPLETED_ID_RE.findall(text or "")


def completed_agent_reports(text: str | None) -> dict[str, str]:
    """Return ``{agent_id: final report}`` for the completions named in *text*.

    A sub-agent's own chain carries no id, so its report — quoted verbatim in the
    parent's notification — is what identifies its chain when several sub-agents
    share one task.

    :param text: A ``<subagent_completion_notification>`` body (or any text that
        embeds one).
    :returns: Completed sub-agent id -> its report text (possibly empty).
    """
    return {
        agent_id: report.strip() for agent_id, report in _COMPLETION_REPORT_RE.findall(text or "")
    }


def load_message_nodes(db_path: Path, session_id: str) -> list[_JsonObject]:
    """Read one session's message forest from Devin's SQLite store, read-only.

    :param db_path: Path to ``sessions.db``.
    :param session_id: Devin session id whose ``message_nodes`` to load.
    :returns: ``[{"node_id", "parent_node_id", "chat_message"}]`` for the
        session, or ``[]`` if the DB is missing/locked/unreadable. Opened
        ``mode=ro`` with a short busy timeout so a live Devin writer is never
        blocked and a transient lock degrades to empty rather than raising.
    """
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
    except sqlite3.Error:
        return []
    try:
        con.execute("PRAGMA busy_timeout=2000")
        rows = con.execute(
            "SELECT node_id, parent_node_id, chat_message "
            "FROM message_nodes WHERE session_id = ? ORDER BY node_id",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    nodes: list[_JsonObject] = []
    for node_id, parent_node_id, chat_message in rows:
        try:
            message = json.loads(chat_message)
        except (TypeError, ValueError):
            continue
        nodes.append(
            {"node_id": node_id, "parent_node_id": parent_node_id, "chat_message": message}
        )
    return nodes


def _content(node: Mapping[str, object]) -> str:
    message = node.get("chat_message")
    if isinstance(message, Mapping):
        text = message.get("content")
        if isinstance(text, str):
            return text
    return ""


def _role(node: Mapping[str, object]) -> str:
    message = node.get("chat_message")
    if isinstance(message, Mapping):
        role = message.get("role")
        if isinstance(role, str):
            return role
    return ""


def reconstruct_transcript_chains(
    nodes: Sequence[_JsonObject], task: str
) -> list[list[_JsonObject]]:
    """Return every executed sub-agent chain whose first user node is *task*.

    One list per sub-agent: Devin gives each ``run_subagent`` delegate its own
    chain, so N delegates launched with the SAME task text produce N chains that
    only their content tells apart. Chains are grouped by their task node (the
    chain root) and the longest leaf-walk per root wins — a root's compaction
    snapshots are strictly shorter than the chain they copied. Roots that never
    produced assistant text are dropped (snapshot stubs, not runs). Each chain is
    sliced to start at its task node, so the ``"You are a subagent"`` system
    prefix is dropped and the child transcript opens with the sub-agent's prompt.

    :param nodes: A session's ``message_nodes`` (as :func:`load_message_nodes`
        returns), in ``node_id`` order.
    :param task: The ``run_subagent`` ``task`` text, which equals the sub-agent's
        first user message verbatim.
    :returns: Chains in Devin's append order (i.e. spawn order), or ``[]`` when
        none has run yet.
    """
    by_id: dict[object, _JsonObject] = {n["node_id"]: n for n in nodes}
    order: dict[object, int] = {n["node_id"]: i for i, n in enumerate(nodes)}
    has_child: set[object] = {
        n["parent_node_id"] for n in nodes if n["parent_node_id"] is not None
    }
    leaves = [node_id for node_id in by_id if node_id not in has_child]

    def chain_up(leaf: object) -> list[object]:
        chain: list[object] = []
        cursor: object | None = leaf
        seen: set[object] = set()
        while cursor is not None and cursor in by_id and cursor not in seen:
            seen.add(cursor)
            chain.append(cursor)
            cursor = by_id[cursor]["parent_node_id"]
        chain.reverse()
        return chain

    longest_by_root: dict[object, list[object]] = {}
    for leaf in leaves:
        chain = chain_up(leaf)
        start = next(
            (
                i
                for i, x in enumerate(chain)
                if _role(by_id[x]) == "user" and _content(by_id[x]) == task
            ),
            None,
        )
        if start is None:
            continue
        sliced = chain[start:]
        root = sliced[0]
        if len(sliced) > len(longest_by_root.get(root, [])):
            longest_by_root[root] = sliced
    return [
        [by_id[x] for x in sliced]
        for _root, sliced in sorted(longest_by_root.items(), key=lambda kv: order[kv[0]])
        if any(_role(by_id[x]) == "assistant" and _content(by_id[x]).strip() for x in sliced)
    ]


def reconstruct_transcript_nodes(nodes: Sequence[_JsonObject], task: str) -> list[_JsonObject]:
    """Return one canonical node chain for the sub-agent whose task is *task*.

    Convenience wrapper over :func:`reconstruct_transcript_chains` that keeps the
    longest chain. Prefer the plural form when several sub-agents may share a task
    text, or they all collapse onto one transcript.

    :param nodes: A session's ``message_nodes``.
    :param task: The ``run_subagent`` ``task`` text.
    :returns: The transcript nodes from the task node to the chain leaf, or ``[]``
        when no chain matches (e.g. the sub-agent has not run yet).
    """
    chains = reconstruct_transcript_chains(nodes, task)
    return max(chains, key=len) if chains else []


def final_assistant_text(chain: Sequence[_JsonObject]) -> str:
    """Return *chain*'s last non-empty assistant message text."""
    text = ""
    for node in chain:
        if _role(node) == "assistant" and _content(node).strip():
            text = _content(node)
    return text


def chain_index_for_report(
    chains: Sequence[Sequence[_JsonObject]],
    report: str,
    claimed: Collection[int] = (),
) -> int | None:
    """Return which of *chains* produced *report*, skipping *claimed* ones.

    Same-task sub-agents are told apart only by content, and the parent's
    completion notification quotes each one's final message — so match that against
    each chain's last assistant text. Falls back to the first unclaimed chain
    (spawn order) when nothing matches, so a child still mirrors a real transcript
    rather than none.

    :param chains: Candidate chains from :func:`reconstruct_transcript_chains`.
    :param report: The agent's report from :func:`completed_agent_reports`.
    :param claimed: Indices already assigned to other sub-agents.
    :returns: Index into *chains*, or ``None`` when every chain is claimed.
    """
    wanted = " ".join(report.split())
    if wanted:
        for index, chain in enumerate(chains):
            if index in claimed:
                continue
            final = " ".join(final_assistant_text(chain).split())
            if final and (final == wanted or final in wanted or wanted in final):
                return index
    return next((i for i in range(len(chains)) if i not in claimed), None)


def transcript_items(
    chain_nodes: Sequence[_JsonObject], agent_name: str
) -> list[tuple[str, _JsonObject]]:
    """Convert a sub-agent node chain into ``external_conversation_item`` payloads.

    Emits the same ``(item_type, item_data)`` shapes the devin-native forwarder
    posts for the parent, so the caller just forwards them to the child session:

    * ``user`` node -> ``message`` (``input_text``);
    * ``assistant`` node -> optional ``reasoning`` (from ``thinking``), one
      ``function_call`` per ``tool_calls`` entry, then a ``message``
      (``output_text``) when it carries text;
    * ``tool`` node -> ``function_call_output`` keyed on its ``tool_call_id``;
    * ``system`` nodes (the rules/prefix blobs) are dropped.

    :param chain_nodes: Nodes from :func:`reconstruct_transcript_nodes`.
    :param agent_name: The devin-native agent name stamped on assistant items.
    :returns: Ordered ``(item_type, item_data)`` pairs ready for the events API.
    """
    items: list[tuple[str, _JsonObject]] = []
    for node in chain_nodes:
        message = node.get("chat_message")
        if not isinstance(message, Mapping):
            continue
        role = _role(node)
        content = _content(node)
        if role == "user":
            if content.strip():
                items.append(
                    (
                        "message",
                        {"role": "user", "content": [{"type": "input_text", "text": content}]},
                    )
                )
        elif role == "assistant":
            thinking = message.get("thinking")
            if isinstance(thinking, Mapping):
                reasoning = thinking.get("thinking")
                if isinstance(reasoning, str) and reasoning.strip():
                    items.append(
                        (
                            "reasoning",
                            # ReasoningData requires `summary`; posting only
                            # `content` 400s the child's /events POST and aborts
                            # the sub-agent mirror, leaving the child with just the
                            # (repeated) task and no real transcript.
                            {
                                "agent": agent_name,
                                "summary": [{"type": "summary_text", "text": reasoning}],
                            },
                        )
                    )
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, Sequence):
                for call in tool_calls:
                    if not isinstance(call, Mapping):
                        continue
                    call_id = call.get("id")
                    if not isinstance(call_id, str) or not call_id:
                        continue
                    items.append(
                        (
                            "function_call",
                            {
                                "agent": agent_name,
                                "name": str(call.get("name") or "tool"),
                                "arguments": json.dumps(
                                    call.get("arguments") or {}, ensure_ascii=False
                                ),
                                "call_id": call_id,
                            },
                        )
                    )
            if content.strip():
                items.append(
                    (
                        "message",
                        {
                            "role": "assistant",
                            "agent": agent_name,
                            "content": [{"type": "output_text", "text": content}],
                        },
                    )
                )
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                items.append(("function_call_output", {"call_id": call_id, "output": content}))
    return items
