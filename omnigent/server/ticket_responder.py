"""Pick the one agent that answers a person's ticket comment.

A person usually means one agent, so the server wakes one instead of every linked
session. ``@Name`` mentions are delivered separately to the named identities; this
module ranks the rest: the agent whose open needs-human ask or latest comment the
person is answering, then the assignee, then the ticket's active worker, then the
Reviewer, then the project's router.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

ASSIGNEE_LABEL = "assignee"
# Stands for the project's router in a responder order: leave the comment to it.
ROUTER = None


def responder_order(
    *,
    linked: Sequence[str],
    routers: Iterable[str],
    reviewer: str | None,
    assignee: str | None,
    asker: str | None,
    commenters: Sequence[str],
    in_review: bool = False,
) -> list[str | None]:
    """Sessions to try, best first, ending with :data:`ROUTER`.

    :param linked: Sessions linked to the ticket, oldest link first.
    :param routers: Sessions that act as the project's router (its Lead).
    :param reviewer: The Reviewer identity's session, if the project has one.
    :param assignee: The session of the identity named by the ``assignee`` label.
    :param asker: The session that raised the ticket's open needs-human ask.
    :param commenters: Sessions that commented on the ticket, most recent first.
    :param in_review: Whether the ticket is in the Review stage.
    :returns: Candidates in order; the caller delivers to the first that accepts.
    """
    router_ids = set(routers)

    def as_candidate(session_id: str) -> str | None:
        return ROUTER if session_id in router_ids else session_id

    order: list[str | None] = []
    # Outside Review, the Reviewer's approval is rarely what a person is answering.
    latest = next(
        (s for s in commenters if in_review or s != reviewer),
        None,
    )
    # Answering an ask or the latest agent comment goes back to whoever wrote it.
    for session_id in (asker, latest, assignee):
        if session_id:
            order.append(as_candidate(session_id))
    workers = [s for s in linked if s not in router_ids and s != reviewer]
    # The active worker: the one that commented most recently, else the newest link.
    recency = {session_id: index for index, session_id in enumerate(commenters)}
    newest_link = {session_id: index for index, session_id in enumerate(reversed(workers))}
    workers.sort(key=lambda s: (recency.get(s, len(recency)), newest_link[s]))
    order.extend(workers)
    if reviewer and reviewer in linked:
        order.append(reviewer)
    order.append(ROUTER)
    return list(dict.fromkeys(order))


def assignee_name(labels: Iterable[dict[str, Any]] | None) -> str | None:
    """The identity name in the ticket's ``assignee`` label, if set."""
    for label in labels or ():
        if label.get("key") == ASSIGNEE_LABEL and isinstance(label.get("value"), str):
            return label["value"].strip() or None
    return None
