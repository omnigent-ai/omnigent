"""PRs a shell create printed count as created only once GitHub confirms they are new."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnigent.runner import github_resource, pr_observer
from omnigent.runner.git_providers import github_created
from omnigent.runner.git_providers.github_created import created_prs
from omnigent.runner.pr_observer import observe_hook, observe_tool_completion
from omnigent.runner.session_prs import SessionPrRegistry

NEW = "https://github.com/example/one/pull/41"
NEWER = "https://github.com/example/one/pull/42"
OLD = "https://github.com/example/one/pull/7"
TEAMMATE = "https://github.com/example/one/pull/43"
MALFORMED = "https://github.com/example/one/pull/44"
MISSING = "https://github.com/example/one/pull/45"
UNKNOWN_HOST = "https://evil.example/example/one/pull/41"


def _opened(age: timedelta) -> str:
    return (datetime.now(UTC) - age).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Answer lookups for 41, 42 (new), 7 (old), 43 (a teammate's) and 44 (malformed)."""
    opened = {
        "repos/example/one/pulls/41": "me " + _opened(timedelta(seconds=30)),
        # The runner's clock lags GitHub's, so this PR looks opened in the future.
        "repos/example/one/pulls/42": "Other-Me " + _opened(timedelta(seconds=-60)),
        "repos/example/one/pulls/7": "me " + _opened(timedelta(days=3)),
        "repos/example/one/pulls/43": "teammate " + _opened(timedelta(seconds=10)),
        "repos/example/one/pulls/44": "me yesterday",
    }
    status = {
        "hosts": {
            "github.com": [
                {"login": "me", "active": True, "state": "success"},
                {"login": "other-me", "active": False, "state": "success"},
                {"login": "teammate", "active": False, "state": "error"},
            ]
        }
    }
    calls: list[list[str]] = []

    def fake_gh(args: list[str], **_: object) -> tuple[int, str, str]:
        calls.append(args)
        if args[:2] == ["auth", "status"]:
            return 0, json.dumps(status), ""
        if (value := opened.get(args[3])) is None:
            return 1, "", "HTTP 404"
        return 0, value + "\n", ""

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    return calls


def _pr_lookups(calls: list[list[str]]) -> list[str]:
    return [args[3] for args in calls if args[0] == "api"]


@contextmanager
def _drained_lookups(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run deferred lookups on a private pool and wait for them before returning."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(pr_observer, "_lookup_pool", pool)
        yield


@pytest.fixture
def observe_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh: list[list[str]]
) -> Callable[[str, str], dict[str, str]]:
    """Report a Claude Bash call whose metadata names NEWER as created; return the registry."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))

    def observe(command: str, stdout: str) -> dict[str, str]:
        with _drained_lookups(monkeypatch):
            observe_hook(
                "conv_create",
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                    "tool_response": {
                        "stdout": stdout,
                        "stderr": "",
                        "interrupted": False,
                        "gitOperation": {"pr": {"url": NEWER, "action": "created"}},
                    },
                    "tool_use_id": "create",
                },
            )
        entries = SessionPrRegistry("conv_create").list()
        return {entry.url: entry.relationship for entry in entries}

    return observe


def test_only_prs_a_local_account_just_opened_count(gh: list[list[str]]) -> None:
    stdout = (
        f"true\n{OLD}\n{MISSING}\n{MALFORMED}\n{NEW}\nsee {OLD} for context\n"
        f"{TEAMMATE}\n{UNKNOWN_HOST}\n{NEWER}\n"
    )

    assert [ref.url for ref in created_prs({"stdout": stdout})] == [NEW, NEWER]
    assert gh[1] == [
        "api",
        "--hostname",
        "github.com",
        "repos/example/one/pulls/7",
        "--jq",
        '[.user.login, .created_at] | join(" ")',
    ]
    # A host no local account signs in to never receives a request.
    assert all(args[2] == "github.com" for args in gh if args[0] == "api")


@pytest.mark.parametrize(
    "result,mixed",
    [
        ({"stdout": NEW, "exit_code": 1}, True),
        ({"stdout": NEW, "interrupted": True}, True),
        ({"stdout": "true"}, True),
        (
            {
                "stdout": "\n".join(
                    f"https://github.com/example/one/pull/{n}" for n in range(1, 22)
                )
            },
            True,
        ),
        # Without reads or Claude's metadata, the immediate scan already read stdout.
        ({"stdout": f"{NEW}\n{NEWER}"}, False),
        # Diagnostics in a separate stderr do not name created PRs.
        ({"stdout": "", "stderr": f"{NEW}\n", "exit_code": 0}, True),
        ({"stderr": f"{NEW}\n", "exit_code": 0}, True),
        # The metadata's own PR is already recorded.
        ({"stdout": NEW, "gitOperation": {"pr": {"url": NEW, "action": "created"}}}, False),
    ],
)
def test_lookups_that_cannot_add_a_pr_make_no_requests(
    gh: list[list[str]], result: object, mixed: bool
) -> None:
    assert created_prs(result, mixed=mixed) == []
    assert gh == []


def test_candidates_share_one_deadline(
    gh: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter([0.0, 1.0, 6.0])
    monkeypatch.setattr(github_created.time, "monotonic", lambda: next(clock, 6.0))

    assert [ref.url for ref in created_prs({"stdout": f"{NEW}\n{NEWER}"})] == [NEW]
    assert _pr_lookups(gh) == ["repos/example/one/pulls/41"]


@pytest.mark.parametrize(
    "command,stdout",
    [
        (
            # A read shares stdout, so the immediate scan skips it.
            "gh api repos/example/one --jq '.permissions.push' && "
            'create() { (cd /work && gh pr create --head "$1" --fill); }; create a; create b',
            f"true\n{NEW}\n{NEWER}",
        ),
        # Claude's metadata names only the last PR a repeated create opened.
        ('for head in a b; do cd /work && gh pr create --head "$head"; done', f"{NEW}\n{NEWER}"),
        # An old PR printed beside the create stays out.
        (f"printf '%s\\n' {OLD} && gh pr create --fill", f"{OLD}\n{NEWER}"),
    ],
)
def test_batched_creates_record_every_new_pr(
    observe_create: Callable[[str, str], dict[str, str]], command: str, stdout: str
) -> None:
    expected = [NEWER] if OLD in stdout else [NEW, NEWER]
    assert observe_create(command, stdout) == dict.fromkeys(expected, "created")


@pytest.mark.parametrize(
    "named",
    [
        "gh api repos/example/one/pulls/41 -X PATCH -f title=x --jq .html_url",
        "gh pr view 41 -R example/one --json url --jq .url",
        "gh pr view 41 --json url --jq .url",
        "gh pr view '#41' --json url --jq .url",
        "gh api 'repos/{owner}/{repo}/pulls/41' --jq .html_url",
        "gh pr view --json url --jq .url -- 41",
        f"gh pr view --json url --jq .url -- {NEW}",
    ],
)
def test_prs_the_call_names_are_not_recovered_as_created(
    observe_create: Callable[[str, str], dict[str, str]], gh: list[list[str]], named: str
) -> None:
    """A PR a command names existed before the call, however recently it was opened."""
    relationships = observe_create(f"{named} && gh pr create --fill", f"{NEW}\n{NEWER}")
    # An edit is recorded as worked on; a read is not recorded at all.
    assert relationships.get(NEW) == ("worked_on" if "PATCH" in named else None)
    assert NEWER in relationships
    assert "repos/example/one/pulls/41" not in _pr_lookups(gh)


@pytest.mark.parametrize("sandbox", [False, True])
def test_a_private_pr_is_read_as_the_account_that_can_see_it(
    monkeypatch: pytest.MonkeyPatch, sandbox: bool
) -> None:
    """The default account cannot see the repo; another signed-in account can."""
    status = {
        "hosts": {
            "github.com": [
                {"login": "work", "active": True, "state": "success"},
                {"login": "me", "active": False, "state": "success"},
            ]
        }
    }
    calls: list[tuple[list[str], str | None]] = []

    def fake_gh(args: list[str], *, token: str | None = None, **_: object) -> tuple[int, str, str]:
        calls.append((args, token))
        if args[:2] == ["auth", "status"]:
            return 0, json.dumps(status), ""
        if args[:2] == ["auth", "token"]:
            return 0, f"token-{args[3]}\n", ""
        if token == "token-me":
            return 0, "me " + _opened(timedelta(seconds=30)), ""
        return 1, "", "HTTP 404"

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource, "_in_sandbox", lambda: sandbox)

    assert [ref.url for ref in created_prs({"stdout": f"{NEW}\n{NEW}"})] == (
        [] if sandbox else [NEW]
    )
    lookups = [token for args, token in calls if args[0] == "api"]
    assert lookups == ([None] if sandbox else [None, "token-me"])


@pytest.mark.parametrize(
    "envelope",
    [
        {"stdout": "true\n", "stderr": f"{NEW}\n", "exit_code": 0},
        {"stdout": f"true\n{NEW}\n", "stderr": "", "exit_code": 1},
    ],
)
def test_serialized_shell_envelopes_keep_stderr_and_failures_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh: list[list[str]], envelope: dict
) -> None:
    """``sys_os_shell`` returns its envelope as JSON text; stderr and exit codes still apply."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    with _drained_lookups(monkeypatch):
        observe_tool_completion(
            "conv_serialized",
            tool_name="sys_os_shell",
            arguments={"command": "gh api repos/example/one --jq .name && gh pr create --fill"},
            result=json.dumps(envelope),
            call_id="serialized-create",
        )
    assert SessionPrRegistry("conv_serialized").list() == []
    assert _pr_lookups(gh) == []


@pytest.mark.parametrize(
    "selector",
    [
        # A branch named "²": ``str.isdigit`` accepts it, ``int`` does not.
        "gh pr view ²",
        # Longer than ``int`` converts by default.
        "gh pr view " + "9" * 5000,
        "gh api repos/example/one/pulls/" + "9" * 5000,
    ],
)
def test_unparsable_pr_selectors_keep_the_created_pr(
    observe_create: Callable[[str, str], dict[str, str]], selector: str
) -> None:
    """A selector the observer cannot read as a PR number must not hide the call's create."""
    relationships = observe_create(f"{selector}; cd /work && gh pr create --fill", NEWER)
    assert relationships == {NEWER: "created"}
