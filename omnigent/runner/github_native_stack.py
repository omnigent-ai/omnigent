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

_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_NODE_ID = re.compile(r"[A-Za-z0-9_=-]{1,256}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
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
# A member's fields that may be hidden from this account while the PR itself is visible,
# and the answer's fields each one leaves out.
_OPTIONAL_PARTS = {
    ("headRepository",): ("head_repository",),
    ("baseRepository",): ("base_repository", "base_default_branch"),
    ("baseRepository", "defaultBranchRef"): ("base_default_branch",),
}

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
    return value if isinstance(value, str) else None


def _ref_name(value: Any) -> str | None:
    """A branch name: 1 to 255 bytes, with no control characters."""
    if not isinstance(value, str) or not value or len(value.encode()) > MAX_REF_BYTES:
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
    type_name = extensions.get("typeName")
    if type_name not in _STACK_FIELDS:
        return False
    fields = _STACK_FIELDS[type_name]
    return fields is None or extensions.get("fieldName") in fields


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
    """An entry's PR as the page reads it: ``None`` where it can't be trusted or is hidden."""
    if () in hidden or not isinstance(node, dict):
        return {"pullRequest": None}
    left_out = {
        field for part, fields in _OPTIONAL_PARTS.items() if part in hidden for field in fields
    }
    if any(part not in _OPTIONAL_PARTS for part in hidden):
        # A field the PR can't be shown without is hidden, so the PR is a gap.
        return {"pullRequest": None}
    try:
        found = PullRequestRef.from_url(node["url"]) if isinstance(node.get("url"), str) else None
    except ValueError:
        found = None
    if found is None or found.provider != "github":
        return {"pullRequest": None}
    if found.host != reference.host or found.repository.lower() != reference.repository.lower():
        return {"pullRequest": None, "outside_repository": True}
    number = _count(node.get("number"), least=1, most=2**63 - 1)
    head_ref = _ref_name(node.get("headRefName"))
    state = node.get("state")
    if number != found.number or head_ref is None or state not in _STATES:
        return {"pullRequest": None}
    if not isinstance(node.get("isDraft"), bool):
        return {"pullRequest": None}
    title = _text(node.get("title")) or ""
    head_repository = node.get("headRepository")
    base_repository = node.get("baseRepository")
    base = base_repository if isinstance(base_repository, dict) else {}
    default_branch = base.get("defaultBranchRef")
    review = node.get("reviewDecision")
    member = {
        # The canonical URL, never GitHub's string.
        "url": found.url,
        "number": number,
        "title": title[:MAX_TITLE_CHARACTERS],
        "state": state,
        "is_draft": node["isDraft"],
        "updated_at": _matching(_TIMESTAMP, node.get("updatedAt")),
        "review_decision": review if review in _REVIEW_DECISIONS else None,
        "head_ref": head_ref,
        "head_sha": _matching(_SHA, node.get("headRefOid")),
        "base_ref": _ref_name(node.get("baseRefName")),
        "base_sha": _matching(_SHA, node.get("baseRefOid")),
        "head_repository": _repository_name(
            head_repository.get("nameWithOwner") if isinstance(head_repository, dict) else None
        ),
        "base_repository": _repository_name(base.get("nameWithOwner")),
        "base_default_branch": _ref_name(
            default_branch.get("name") if isinstance(default_branch, dict) else None
        ),
    }
    return {"pullRequest": {**member, **dict.fromkeys(left_out)}}


def _stack(stack: Any, reference: PullRequestRef, errors: list[Any]) -> dict[str, Any] | None:
    """The stack as the page reads it, or ``None`` for a PR in none.

    :raises PullRequestError: For a stack whose identity or shape isn't GitHub's.
    """
    if stack is None:
        if errors:
            raise _unreadable()
        return None
    if not isinstance(stack, dict):
        raise _unreadable()
    entries = stack.get("entries")
    page = entries.get("pageInfo") if isinstance(entries, dict) else None
    nodes = entries.get("nodes") if isinstance(entries, dict) else None
    has_next_page = page.get("hasNextPage") if isinstance(page, dict) else None
    shaped = {
        "id": _matching(_NODE_ID, stack.get("id")),
        "number": _count(stack.get("number"), least=1, most=2**63 - 1),
        "size": _count(stack.get("size"), least=0, most=MAX_STACK_SIZE),
        "baseRefName": _ref_name(stack.get("baseRefName")),
    }
    if any(value is None for value in shaped.values()) or not isinstance(has_next_page, bool):
        raise _unreadable()
    if not isinstance(nodes, list):
        raise _unreadable()
    hidden = _hidden_parts(errors, len(nodes))
    shaped_nodes = [
        {
            "position": node["position"],
            **_member(node.get("pullRequest"), reference, hidden.get(index, set())),
        }
        for index, node in enumerate(nodes[:MAX_NATIVE_STACK_ENTRIES])
        if isinstance(node, dict)
        and _count(node.get("position"), least=-(2**31), most=2**31) is not None
    ]
    return {**shaped, "entries": {"hasNextPage": has_next_page, "nodes": shaped_nodes}}


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
    errors = response.get("errors") or []
    if not isinstance(errors, list):
        raise _unreadable()
    if errors and all(_lacks_stacks(error) for error in errors):
        return {"object": "pull_request_native_stack", "supported": False, "viewer": None}
    data = response.get("data")
    viewer = data.get("viewer") if isinstance(data, dict) else None
    login = _matching(_LOGIN, viewer.get("login") if isinstance(viewer, dict) else None)
    repository = data.get("repository") if isinstance(data, dict) else None
    pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
    if login is None or not isinstance(pull_request, dict):
        raise _unreadable()
    if _count(pull_request.get("number"), least=1, most=2**63 - 1) != reference.number:
        raise _unreadable()
    entry = pull_request.get("stackEntry")
    position = entry.get("position") if isinstance(entry, dict) else None
    if entry is not None and _count(position, least=-(2**31), most=2**31) is None:
        raise _unreadable()
    return {
        "object": "pull_request_native_stack",
        "supported": True,
        "viewer": login,
        "native_stack": {
            "stackEntry": None if entry is None else {"position": position},
            "stack": _stack(pull_request.get("stack"), reference, errors),
        },
    }
