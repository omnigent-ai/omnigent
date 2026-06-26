"""Job and Run entities.

A *Job* is a saved AI workflow authored as a node graph in the web UI. Its
execution model is *promptgen*: the graph is rendered to an English narrative
(client-side, by ``flowToText.ts``), persisted on the job, and fed as the
initial prompt to a single agent session. A *Run* records one such execution —
it *is* an agent session.

The backend never interprets the graph; it stores it as opaque JSON. See
``designs`` / the "Omnigent jobs" brief for the rationale.
"""

from __future__ import annotations

from dataclasses import dataclass

# Run lifecycle states. ``running`` is recorded at creation; terminal states are
# reconciled from the underlying session on read.
RUN_STATUS_RUNNING = "running"
RUN_STATUS_FINISHED = "finished"
RUN_STATUS_FAILED = "failed"


@dataclass
class Job:
    """
    A saved AI workflow executed via promptgen.

    :param id: Unique job identifier, e.g. ``"job_0f1a2b3c..."``.
    :param created_at: Unix epoch seconds of creation.
    :param updated_at: Unix epoch seconds of the last update.
    :param name: Human-readable job name.
    :param graph: Opaque flow-graph JSON string (nodes/edges/loops). The
        backend never parses this; it is round-tripped to the client.
    :param narrative: English narrative rendered from ``graph`` by the
        client. This is the prompt fed to the agent on a run.
    :param agent_id: The agent this job runs as, or ``None`` if not yet
        bound (a run then falls back to a default built-in agent).
    :param harness_override: Optional harness to run with, overriding the
        agent's default executor.
    :param model_override: Optional model id to run with.
    :param created_by: Owning user id, or ``None`` in single-user mode.
    :param schedule_config: Reserved for future scheduling (cron/loops);
        opaque JSON, unused in v1.
    """

    id: str
    created_at: int
    updated_at: int
    name: str
    graph: str
    narrative: str
    agent_id: str | None = None
    harness_override: str | None = None
    model_override: str | None = None
    created_by: str | None = None
    schedule_config: str | None = None


@dataclass
class Run:
    """
    One execution of a :class:`Job` — an agent session.

    :param id: Unique run identifier, e.g. ``"run_0f1a2b3c..."``.
    :param job_id: The job this run belongs to.
    :param session_id: The conversation/session created for this run, or
        ``None`` if the session has since been deleted.
    :param status: One of ``running`` / ``finished`` / ``failed``.
    :param started_at: Unix epoch seconds the run was triggered.
    :param completed_at: Unix epoch seconds the run reached a terminal
        state, or ``None`` while still running.
    :param error: Failure detail when ``status == "failed"``, else ``None``.
    :param created_by: Owning user id, or ``None`` in single-user mode.
    """

    id: str
    job_id: str
    session_id: str | None
    status: str
    started_at: int
    completed_at: int | None = None
    error: str | None = None
    created_by: str | None = None
