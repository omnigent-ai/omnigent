"""GitHub's own record of the stack a pull request is in, where its host has stacks.

The host is checked before ``gh`` runs, so a token in the environment never goes to a host
taken from a pull request URL. What GitHub answers is untrusted: every field is checked and
capped, a member outside the pull request's repository is blanked, and a client shows titles
as plain text. An entry or field GitHub hides from this account is a gap, never a failure.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from omnigent.runner import github_resource as github
from omnigent.runner.pull_request_errors import PullRequestError
from omnigent.runner.session_prs import PullRequestRef

# The most stack entries one read lists; a longer stack reads as partial.
MAX_NATIVE_STACK_ENTRIES = 50
# GitHub caps titles at 256 characters, so this never cuts a real one.
MAX_TITLE_CHARACTERS = 1024
# A ref name's limit on common filesystems.
MAX_REF_BYTES = 255
MAX_REPOSITORY_CHARACTERS = 256
MAX_STACK_SIZE = 10_000
_UNKNOWN_HOST = "Sign in to this GitHub host with gh before reading its stacks"
_UNREADABLE = "GitHub couldn't read this pull request's stack. Try again."

# A user's login, or a GitHub App's, which ends in ``[bot]``.
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}(?:\[bot\])?")
# GraphQL's Int is 32-bit and signed; the answer's Struct keeps such numbers exactly.
_INT_MIN, _INT_MAX = -(2**31), 2**31 - 1
_NODE_ID = re.compile(r"[A-Za-z0-9_=-]{1,256}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_SHA = re.compile(r"[0-9a-f]{40}")
_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})
_REVIEW_DECISIONS = frozenset({"APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"})
# The fields a host without stacks lacks, by the type GitHub names in ``undefinedField``.
_STACK_FIELDS = {
    "PullRequest": frozenset({"stack", "stackEntry"}),
    "PullRequestStack": None,
    "PullRequestStackEntry": None,
}
_HIDDEN = frozenset({"FORBIDDEN", "NOT_FOUND"})
_ENTRIES_PATH = ("repository", "pullRequest", "stack", "entries", "nodes")
# A member's parts that may be hidden from this account while the PR itself is visible, by
# the start of the hidden path, and the answer's fields each one leaves out. The longest
# matching start applies; a hidden path no start matches makes the member a gap.
_OPTIONAL_PARTS = {
    ("headRepository",): ("head_repository",),
    ("baseRepository",): ("base_repository", "base_default_branch"),
    ("baseRepository", "nameWithOwner"): ("base_repository",),
    ("baseRepository", "defaultBranchRef"): ("base_default_branch",),
}


@dataclass(frozen=True)
class _Object:
    """An object the query asks for: every key it names must be in the answer."""

    fields: dict[str, Any] = field(default_factory=dict)
    nullable: bool = False


@dataclass(frozen=True)
class _List:
    item: _Object


# A scalar: present, with its value checked where it's shaped.
_SCALAR = None
_REPOSITORY_OBJECT = _Object({"nameWithOwner": _SCALAR}, nullable=True)
_MEMBER = _Object(
    {
        **dict.fromkeys(
            (
                "url",
                "number",
                "title",
                "state",
                "isDraft",
                "updatedAt",
                "reviewDecision",
                "headRefName",
                "headRefOid",
                "baseRefName",
                "baseRefOid",
            ),
            _SCALAR,
        ),
        "headRepository": _REPOSITORY_OBJECT,
        "baseRepository": _Object(
            {
                "nameWithOwner": _SCALAR,
                "defaultBranchRef": _Object({"name": _SCALAR}, nullable=True),
            },
            nullable=True,
        ),
    },
    # Null where GitHub hides the PR, or it no longer exists.
    nullable=True,
)
_STACK = _Object(
    {
        **dict.fromkeys(("id", "number", "size", "baseRefName"), _SCALAR),
        "entries": _Object(
            {
                "pageInfo": _Object({"hasNextPage": _SCALAR}),
                "nodes": _List(_Object({"position": _SCALAR, "pullRequest": _MEMBER})),
            }
        ),
    },
    nullable=True,
)
# The answer this read accepts. A null GitHub may send elsewhere, such as for a repository
# or pull request it can't find, isn't an answer the read can show.
_DATA = _Object(
    {
        "viewer": _Object({"login": _SCALAR}),
        "repository": _Object(
            {
                "pullRequest": _Object(
                    {
                        "number": _SCALAR,
                        "stackEntry": _Object({"position": _SCALAR}, nullable=True),
                        "stack": _STACK,
                    }
                )
            }
        ),
    }
)


def _conforms(value: Any, shape: _Object | _List | None) -> bool:
    """Whether ``value`` has ``shape``'s objects, lists and keys; scalars are checked later."""
    if shape is _SCALAR:
        return True
    if isinstance(shape, _List):
        return isinstance(value, list) and all(_conforms(item, shape.item) for item in value)
    if value is None:
        return shape.nullable
    return isinstance(value, dict) and all(
        key in value and _conforms(value[key], part) for key, part in shape.fields.items()
    )


_QUERY = f"""query($owner: String!, $name: String!, $number: Int!) {{
  viewer {{ login }}
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      number
      stackEntry {{ position }}
      stack {{
        id number size baseRefName
        entries(first: {MAX_NATIVE_STACK_ENTRIES}) {{
          pageInfo {{ hasNextPage }}
          nodes {{ position pullRequest {{
            url number title state isDraft updatedAt reviewDecision
            headRefName headRefOid baseRefName baseRefOid
            headRepository {{ nameWithOwner }}
            baseRepository {{ nameWithOwner defaultBranchRef {{ name }} }}
          }} }}
        }}
      }}
    }}
  }}
}}"""


def _unreadable() -> PullRequestError:
    return PullRequestError(_UNREADABLE)


def _text(value: Any) -> str | None:
    """``value`` if it's text that encodes as UTF-8; JSON may carry an unpaired surrogate."""
    if not isinstance(value, str):
        return None
    try:
        value.encode()
    except UnicodeEncodeError:
        return None
    return value


def _one_of(value: Any, allowed: frozenset[str]) -> str | None:
    """``value`` if it's one of ``allowed``; any other type, list or object included, isn't."""
    return value if isinstance(value, str) and value in allowed else None


def _ref_name(value: Any) -> str | None:
    """A branch name: 1 to 255 bytes, with no control characters."""
    value = _text(value)
    if not value or len(value.encode()) > MAX_REF_BYTES:
        return None
    return (
        None if any(ord(character) < 32 or ord(character) == 127 for character in value) else value
    )


def _repository_name(value: Any) -> str | None:
    """``owner/repo`` as GitHub names repositories, or ``None``."""
    name = _text(value)
    if name is None or len(name) > MAX_REPOSITORY_CHARACTERS or not _REPOSITORY.fullmatch(name):
        return None
    return None if any(part in {".", ".."} for part in name.split("/")) else name


def _matching(pattern: re.Pattern[str], value: Any) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _timestamp(value: Any) -> str | None:
    """A UTC time as GitHub writes one, which must also be a real date and time."""
    if _matching(_TIMESTAMP, value) is None:
        return None
    try:
        datetime.strptime(value.removesuffix("Z") + "+0000", "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return value


def _left_out(part: tuple[str, ...]) -> tuple[str, ...] | None:
    """The answer's fields a hidden ``part`` of a member leaves out; ``None`` if it's required."""
    starts = [start for start in _OPTIONAL_PARTS if part[: len(start)] == start]
    return _OPTIONAL_PARTS[max(starts, key=len)] if starts else None


def _count(value: Any, *, least: int, most: int) -> int | None:
    is_integer = isinstance(value, int) and not isinstance(value, bool)
    return value if is_integer and least <= value <= most else None


def _error_kind(error: Any) -> str | None:
    if not isinstance(error, dict):
        return None
    extensions = error.get("extensions")
    code = extensions.get("code") if isinstance(extensions, dict) else None
    return _text(error.get("type")) or _text(code)


def _lacks_stacks(error: Any) -> bool:
    """Whether ``error`` is GitHub saying its schema has no stack field this read names."""
    extensions = error.get("extensions") if isinstance(error, dict) else None
    if not isinstance(extensions, dict) or extensions.get("code") != "undefinedField":
        return False
    type_name = _one_of(extensions.get("typeName"), frozenset(_STACK_FIELDS))
    field_name = _text(extensions.get("fieldName"))
    if type_name is None or field_name is None:
        return False
    fields = _STACK_FIELDS[type_name]
    return fields is None or field_name in fields


def _hidden_parts(errors: list[Any], entry_count: int) -> dict[int, set[tuple[str, ...]]]:
    """What GitHub hid in each entry, by index: ``()`` for its whole PR, else a field's path.

    :raises PullRequestError: For any error that isn't one hiding part of an entry's PR.
    """
    hidden: dict[int, set[tuple[str, ...]]] = {}
    for error in errors:
        path = error.get("path") if isinstance(error, dict) else None
        if _error_kind(error) not in _HIDDEN or not isinstance(path, list):
            raise _unreadable()
        prefix, rest = tuple(path[: len(_ENTRIES_PATH)]), path[len(_ENTRIES_PATH) :]
        if prefix != _ENTRIES_PATH or len(rest) < 2 or rest[1] != "pullRequest":
            raise _unreadable()
        index = _count(rest[0], least=0, most=entry_count - 1)
        if index is None or not all(isinstance(part, str) for part in rest[2:]):
            raise _unreadable()
        hidden.setdefault(index, set()).add(tuple(rest[2:]))
    return hidden


def _member(node: Any, reference: PullRequestRef, hidden: set[tuple[str, ...]]) -> dict[str, Any]:
    """An entry's PR as the page reads it: ``None`` where it can't be trusted or is hidden.

    ``node`` has the member's shape already: an object with every key, or null.
    """
    if () in hidden or node is None:
        return {"pullRequest": None}
    parts = [_left_out(part) for part in hidden]
    if None in parts:
        # A field the PR can't be shown without is hidden, so the PR is a gap.
        return {"pullRequest": None}
    left_out = {name for names in parts if names for name in names}
    try:
        found = PullRequestRef.from_url(node["url"]) if _text(node["url"]) else None
    except ValueError:
        found = None
    if found is None or found.provider != "github":
        return {"pullRequest": None}
    if found.host != reference.host or found.repository.lower() != reference.repository.lower():
        return {"pullRequest": None, "outside_repository": True}
    number = _count(node["number"], least=1, most=_INT_MAX)
    head_ref = _ref_name(node["headRefName"])
    state = _one_of(node["state"], _STATES)
    if number != found.number or head_ref is None or state is None:
        return {"pullRequest": None}
    if not isinstance(node["isDraft"], bool):
        return {"pullRequest": None}
    title = _text(node["title"]) or ""
    head_repository = node["headRepository"] or {}
    base = node["baseRepository"] or {}
    default_branch = base.get("defaultBranchRef") or {}
    member = {
        # The canonical URL, never GitHub's string.
        "url": found.url,
        "number": number,
        "title": title[:MAX_TITLE_CHARACTERS],
        "state": state,
        "is_draft": node["isDraft"],
        "updated_at": _timestamp(node["updatedAt"]),
        "review_decision": _one_of(node["reviewDecision"], _REVIEW_DECISIONS),
        "head_ref": head_ref,
        "head_sha": _matching(_SHA, node["headRefOid"]),
        "base_ref": _ref_name(node["baseRefName"]),
        "base_sha": _matching(_SHA, node["baseRefOid"]),
        "head_repository": _repository_name(head_repository.get("nameWithOwner")),
        "base_repository": _repository_name(base.get("nameWithOwner")),
        "base_default_branch": _ref_name(default_branch.get("name")),
    }
    return {"pullRequest": {**member, **dict.fromkeys(left_out)}}


def _stack(stack: Any, reference: PullRequestRef, errors: list[Any]) -> dict[str, Any] | None:
    """The stack as the page reads it, or ``None`` for a PR in none.

    ``stack`` has the stack's shape already: an object with every key, or null.

    :raises PullRequestError: For a stack whose identity, or an entry's position, isn't GitHub's.
    """
    if stack is None:
        if errors:
            raise _unreadable()
        return None
    has_next_page = stack["entries"]["pageInfo"]["hasNextPage"]
    nodes = stack["entries"]["nodes"]
    shaped = {
        "id": _matching(_NODE_ID, stack["id"]),
        "number": _count(stack["number"], least=1, most=_INT_MAX),
        "size": _count(stack["size"], least=0, most=MAX_STACK_SIZE),
        "baseRefName": _ref_name(stack["baseRefName"]),
    }
    if any(value is None for value in shaped.values()) or not isinstance(has_next_page, bool):
        raise _unreadable()
    # A malformed position, dropped quietly, would read as a shorter stack.
    if any(_count(node["position"], least=_INT_MIN, most=_INT_MAX) is None for node in nodes):
        raise _unreadable()
    hidden = _hidden_parts(errors, len(nodes))
    shaped_nodes = [
        {
            "position": node["position"],
            **_member(node["pullRequest"], reference, hidden.get(index, set())),
        }
        for index, node in enumerate(nodes[:MAX_NATIVE_STACK_ENTRIES])
    ]
    # An answer longer than the read asked for is cut, and says it has more.
    more = has_next_page or len(nodes) > MAX_NATIVE_STACK_ENTRIES
    return {**shaped, "entries": {"hasNextPage": more, "nodes": shaped_nodes}}


def pull_request_native_stack(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """GitHub's stack for ``reference``, as the account chosen for its repository reads it.

    ``supported`` is false where the host's schema has no stacks: the client can't say, and
    reads the stack another way. ``native_stack`` holds GitHub's answer in the shape its
    GraphQL API gives, with each member shaped and checked.

    :raises PullRequestError: When the host isn't set up for GitHub, which is refused before
        any ``gh`` call, or when GitHub's answer can't be read; never with GitHub's text.
    """
    if not github._known_github_host(reference.host):
        raise PullRequestError(_UNKNOWN_HOST)
    owner, name = reference.repository.split("/", 1)
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        token = github._pr_token(root, reference)
        # gh exits non-zero when the answer has errors, and still prints it.
        _, output, _ = github._pr_json_write(
            root,
            host=["--hostname", reference.host],
            token=token,
            method="POST",
            endpoint="graphql",
            fields={
                "query": _QUERY,
                "variables": {"owner": owner, "name": name, "number": reference.number},
            },
        )
    try:
        response = json.loads(output)
    except ValueError as exc:
        raise _unreadable() from exc
    if not isinstance(response, dict):
        raise _unreadable()
    errors = response.get("errors")
    if errors is None:
        errors = []
    elif not isinstance(errors, list):
        raise _unreadable()
    if errors and all(_lacks_stacks(error) for error in errors):
        return {"object": "pull_request_native_stack", "supported": False, "viewer": None}
    data: Any = response.get("data")
    # Every object and key the query asks for, before any value is read.
    if not _conforms(data, _DATA):
        raise _unreadable()
    login = _matching(_LOGIN, data["viewer"]["login"])
    pull_request = data["repository"]["pullRequest"]
    if login is None or _count(pull_request["number"], least=1, most=_INT_MAX) != reference.number:
        raise _unreadable()
    entry = pull_request["stackEntry"]
    position = None if entry is None else entry["position"]
    if entry is not None and _count(position, least=_INT_MIN, most=_INT_MAX) is None:
        raise _unreadable()
    return {
        "object": "pull_request_native_stack",
        "supported": True,
        "viewer": login,
        "native_stack": {
            "stackEntry": None if entry is None else {"position": position},
            "stack": _stack(pull_request["stack"], reference, errors),
        },
    }
