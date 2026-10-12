"""A failing git provider cannot change a tool result or hide another provider's PRs.

Fake providers share the dispatcher with GitHub and the real session PR registry.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import types
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

import omnigent.git_providers as provider_registry
from omnigent.git_providers import (
    FacetModules,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
    reset_for_tests,
)
from omnigent.runner import pr_observer
from omnigent.runner.git_providers import ShellPrOp, ShellSegment
from omnigent.runner.git_providers.tool_output import pr_reference, result_objects
from omnigent.runner.pr_observer import extract_prs, observe_tool_completion
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.runner.git_provider_fixtures import register_provider

SESSION = "conv_isolation"
HOST = "git.example.test"
MR = f"https://{HOST}/g/s/p/-/merge_requests/7"
GITHUB_PR = "https://github.com/acme/tools/pull/12"
GH_CREATE = "gh pr create --title T --body B"
GLAB_CREATE = "glab mr create --fill"
# The fake provider that parses merge requests and answers for ``glab`` and its MCP tool.
WORKING = "glab"
MCP_CREATE = f"mcp__{WORKING}__create_merge_request"
OBSERVER_LOGGER = pr_observer.__name__
# A call that creates one PR on each forge, and its combined output.
BOTH_CREATE = f"{GH_CREATE} && {GLAB_CREATE}"
BOTH_OUTPUT = f"{GITHUB_PR}\n{json.dumps({'web_url': MR})}"


@dataclass(frozen=True)
class Forge:
    """A fake forge that claims ``HOST`` and parses its merge requests only when ``parses`` is set.

    Each descriptor method named in ``failing`` raises.
    """

    id: str
    facets: FacetModules
    parses: bool = False
    failing: frozenset[str] = frozenset()
    display_name: str = "Forge"
    default_hosts: tuple[str, ...] = ()

    def _enter(self, method: str) -> None:
        if method in self.failing:
            raise KeyError(method)

    def matches_host(self, host: str, instances: Instances) -> bool:
        self._enter("matches_host")
        return self.parses and host == HOST

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        self._enter("parse_remote_url")
        return None

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        self._enter("parse_pr_url")
        match = re.fullmatch(
            rf"https://{re.escape(HOST)}/(.+)/-/merge_requests/([1-9][0-9]*)", url.strip()
        )
        if not self.parses or match is None:
            return None
        return ParsedPullRequest(self.id, HOST, match[1], int(match[2]), match[0])


class Facet:
    """The observer hooks of a fake forge; each hook named in ``failing`` raises ``error``.

    Its commands are ``<cli> mr create``, it reads a PR from an object's ``web_url``, and it
    claims the MCP tool ``mcp__<cli>__create_merge_request``.
    """

    def __init__(self, cli: str, failing: Collection[str], error: Exception | None) -> None:
        self.cli = cli
        self.failing = frozenset(failing)
        self.error = RuntimeError("hook failed") if error is None else error
        self.calls: list[str] = []

    def _enter(self, hook: str) -> None:
        self.calls.append(hook)
        if hook in self.failing:
            raise self.error

    def shell_pr_operations(self, segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
        self._enter("shell_pr_operations")
        return [
            ShellPrOp(tracks=True, creates=True, target=None, content_only=False)
            for segment in segments
            if segment.invocation_tokens[:3] == (self.cli, "mr", "create")
        ]

    def pr_from_object(self, obj: Mapping[str, object]) -> PullRequestRef | None:
        self._enter("pr_from_object")
        return pr_reference(obj.get("web_url"))

    def mcp_prs(
        self, tool_name: str, arguments: dict[str, object], result: object
    ) -> tuple[list[PullRequestRef], bool] | None:
        self._enter("mcp_prs")
        if tool_name != f"mcp__{self.cli}__create_merge_request":
            return None
        refs = [ref for obj in result_objects(result) if (ref := pr_reference(obj.get("web_url")))]
        return refs, True


Install = Callable[..., Facet]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Use the built-in providers with no host settings, a fresh store, and no past failures."""
    monkeypatch.setattr(provider_registry, "PROVIDER_MODULES", ("omnigent.git_providers.github",))
    monkeypatch.setattr(provider_registry.importlib.metadata, "entry_points", lambda **_: ())
    for name in (
        "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS",
        "GH_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(pr_observer, "_failed_providers", set())
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Install:
    """Return a function that registers a fake provider, after the built-in ones, and its facet."""

    def register(
        provider_id: str,
        *,
        failing: Collection[str] = (),
        error: Exception | None = None,
        parses: bool = False,
    ) -> Facet:
        facet = Facet(provider_id, failing, error)
        module_name = f"tests_pr_observer_isolation_{provider_id}"
        module = types.ModuleType(module_name)
        module.PULL_REQUESTS = facet  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, module_name, module)
        register_provider(Forge(provider_id, FacetModules(pull_requests=module_name), parses))
        return facet

    return register


def install_facet_that_fails_at_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider_id: str, source: str
) -> None:
    """Register a fake provider whose facet module runs ``source`` when it is imported."""
    module_name = f"tests_pr_observer_isolation_{provider_id}"
    (tmp_path / f"{module_name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    register_provider(Forge(provider_id, FacetModules(pull_requests=module_name)))


def shell_result(output: str) -> dict[str, object]:
    """Return the result of a shell command that printed *output* and exited 0."""
    return {"stdout": output + "\n", "exit_code": 0}


def observe(command: str, output: str) -> None:
    observe_tool_completion(
        SESSION,
        tool_name="Bash",
        arguments={"command": command},
        result=shell_result(output),
        call_id="call-1",
    )


def recorded() -> dict[str, str]:
    """Return the URL of each PR in the session, by provider."""
    return {entry.provider: entry.url for entry in SessionPrRegistry(SESSION).list()}


def observer_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == OBSERVER_LOGGER]


# ---------------------------------------------------------------------------
# A provider that raises in a hook
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hook", ["shell_pr_operations", "pr_from_object"])
@pytest.mark.parametrize(
    "error",
    [KeyError("missing"), RuntimeError("boom"), ZeroDivisionError()],
    ids=lambda error: type(error).__name__,
)
def test_github_records_its_pr_when_another_provider_raises(
    install: Install, hook: str, error: Exception
) -> None:
    broken = install("broken", failing={hook}, error=error)

    observe(GH_CREATE, GITHUB_PR)

    assert hook in broken.calls
    assert recorded() == {"github": GITHUB_PR}


@pytest.mark.parametrize("hook", ["shell_pr_operations", "pr_from_object"])
def test_the_providers_after_a_failing_one_still_run(install: Install, hook: str) -> None:
    install("broken", failing={hook}, error=KeyError("missing"))
    install(WORKING, parses=True)

    observe(BOTH_CREATE, BOTH_OUTPUT)

    assert recorded() == {"github": GITHUB_PR, WORKING: MR}


def test_a_failing_shell_hook_answers_with_no_operations(install: Install) -> None:
    install("broken", failing={"shell_pr_operations"})

    refs, created = extract_prs("Bash", {"command": GH_CREATE}, shell_result(GITHUB_PR))

    assert ([ref.url for ref in refs], created) == ([GITHUB_PR], True)


@pytest.mark.parametrize("command", [GLAB_CREATE, BOTH_CREATE])
@pytest.mark.parametrize("mode", ["created", "foreign", "error"])
def test_structured_parser_does_not_fall_back_to_unrelated_output(
    install: Install, monkeypatch: pytest.MonkeyPatch, command: str, mode: str
) -> None:
    facet = install(WORKING, parses=True)

    def parse(result: object) -> list[PullRequestRef]:
        if mode == "error":
            raise RuntimeError("unreadable result")
        assert isinstance(result, dict)
        ref = pr_reference(result["created_pr"])
        assert ref is not None
        return [ref]

    monkeypatch.setattr(
        facet,
        "shell_pr_operations",
        lambda _: [
            ShellPrOp(
                tracks=True, creates=True, target=None, content_only=False, parse_result=parse
            )
        ],
    )
    refs, created = extract_prs(
        "Bash",
        {"command": command},
        {"created_pr": GITHUB_PR if mode == "foreign" else MR, "stdout": GITHUB_PR},
    )
    expected = {MR} if mode == "created" else set()
    if command == BOTH_CREATE:
        expected.add(GITHUB_PR)
    assert {ref.url for ref in refs} == expected
    assert created


def test_a_failing_object_hook_answers_with_no_pr(install: Install) -> None:
    install("broken", failing={"pr_from_object"})
    install(WORKING, parses=True)

    refs, created = extract_prs(
        "Bash", {"command": GLAB_CREATE}, shell_result(json.dumps({"web_url": MR}))
    )

    assert ([ref.url for ref in refs], created) == ([MR], True)


def test_a_failing_mcp_hook_passes_the_tool_to_the_next_provider(install: Install) -> None:
    broken = install("broken", failing={"mcp_prs"}, error=KeyError("missing"))
    install(WORKING, parses=True)

    refs, created = extract_prs(
        MCP_CREATE, {"project": "g/s/p"}, {"structuredContent": {"web_url": MR}}
    )

    assert broken.calls == ["mcp_prs"]
    assert ([ref.url for ref in refs], created) == ([MR], True)


def test_a_failing_mcp_hook_leaves_an_unclaimed_tool_empty(install: Install) -> None:
    broken = install("broken", failing={"mcp_prs"})

    result = extract_prs("mcp__other__tool", {}, {"structuredContent": {"web_url": MR}})

    assert broken.calls == ["mcp_prs"]
    assert result == ([], False)


# ---------------------------------------------------------------------------
# A provider whose facet fails to import
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "raise RuntimeError('facet import failed')\n",
        "raise KeyError('facet import failed')\n",
        "import tests_pr_observer_isolation_absent_dependency\n",
        "def broken(:\n",
    ],
    ids=["runtime-error", "key-error", "missing-dependency", "syntax-error"],
)
def test_a_facet_that_fails_to_import_is_skipped_and_the_others_load(
    install: Install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> None:
    install_facet_that_fails_at_import(monkeypatch, tmp_path, "broken", source)
    install(WORKING, parses=True)

    observe(BOTH_CREATE, BOTH_OUTPUT)

    assert recorded() == {"github": GITHUB_PR, WORKING: MR}


# ---------------------------------------------------------------------------
# The observer never raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [KeyError("missing"), RuntimeError("boom"), AttributeError("no such field")],
    ids=lambda error: type(error).__name__,
)
def test_the_observer_does_not_raise_when_recording_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(SessionPrRegistry, "record", fail)

    with caplog.at_level(logging.WARNING, logger=OBSERVER_LOGGER):
        observe(GH_CREATE, GITHUB_PR)

    [record] = observer_records(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "Failed to record session PRs"
    assert record.exc_info is not None and record.exc_info[1] is error


# ---------------------------------------------------------------------------
# A provider whose descriptor fails
# ---------------------------------------------------------------------------


def test_github_records_its_pr_when_a_provider_module_fails_at_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module_name = "tests_pr_observer_isolation_provider"
    (tmp_path / f"{module_name}.py").write_text(
        "raise RuntimeError('provider import failed')\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    entry = EntryPoint(
        name="broken", value=f"{module_name}:PROVIDER", group=provider_registry.ENTRY_POINT_GROUP
    )
    monkeypatch.setattr(provider_registry.importlib.metadata, "entry_points", lambda **_: (entry,))

    observe(GH_CREATE, GITHUB_PR)

    assert recorded() == {"github": GITHUB_PR}


@pytest.mark.parametrize("method", ["matches_host", "parse_pr_url"])
def test_github_records_its_pr_when_a_descriptor_raises(method: str) -> None:
    # A GitHub-shaped PR URL on the broken forge's host, so the observer consults its descriptor.
    pr_url = f"https://{HOST}/acme/tools/pull/5"
    register_provider(Forge("broken", FacetModules(), parses=True, failing=frozenset({method})))

    observe(GH_CREATE, pr_url)

    assert recorded() == {"github": pr_url}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_a_failing_provider_warns_once_then_logs_at_debug(
    install: Install, caplog: pytest.LogCaptureFixture
) -> None:
    install("broken", failing={"shell_pr_operations"}, error=KeyError("missing"))

    with caplog.at_level(logging.DEBUG, logger=OBSERVER_LOGGER):
        for _ in range(3):
            extract_prs("Bash", {"command": GH_CREATE}, shell_result(GITHUB_PR))

    records = observer_records(caplog)
    assert [record.levelno for record in records] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
    ]
    assert all("broken" in record.getMessage() for record in records)
    assert all(record.exc_info is not None for record in records)
    assert records[0].exc_info is not None and records[0].exc_info[0] is KeyError


def test_each_failing_provider_warns_once(
    install: Install, caplog: pytest.LogCaptureFixture
) -> None:
    install("first", failing={"shell_pr_operations"})
    install("second", failing={"shell_pr_operations"})

    with caplog.at_level(logging.DEBUG, logger=OBSERVER_LOGGER):
        for _ in range(2):
            extract_prs("Bash", {"command": GH_CREATE}, shell_result(GITHUB_PR))

    records = observer_records(caplog)
    levels = {
        name: [record.levelno for record in records if f"provider {name} " in record.getMessage()]
        for name in ("first", "second")
    }
    assert levels == {
        "first": [logging.WARNING, logging.DEBUG],
        "second": [logging.WARNING, logging.DEBUG],
    }


def test_a_provider_warns_once_across_its_hooks(
    install: Install, caplog: pytest.LogCaptureFixture
) -> None:
    install("broken", failing={"shell_pr_operations", "pr_from_object"})

    with caplog.at_level(logging.DEBUG, logger=OBSERVER_LOGGER):
        extract_prs("Bash", {"command": GH_CREATE}, shell_result(GITHUB_PR))

    hooks = [
        (record.levelno, hook)
        for record in observer_records(caplog)
        for hook in ("shell_pr_operations", "pr_from_object")
        if hook in record.getMessage()
    ]
    assert hooks == [(logging.WARNING, "shell_pr_operations"), (logging.DEBUG, "pr_from_object")]


def test_a_facet_import_failure_warns_once_then_logs_at_debug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    install_facet_that_fails_at_import(
        monkeypatch, tmp_path, "broken", "raise RuntimeError('facet import failed')\n"
    )

    with caplog.at_level(logging.DEBUG, logger=OBSERVER_LOGGER):
        for _ in range(2):
            extract_prs("Bash", {"command": GH_CREATE}, shell_result(GITHUB_PR))

    records = observer_records(caplog)
    assert [record.levelno for record in records] == [logging.WARNING, logging.DEBUG]
    assert all("broken" in record.getMessage() for record in records)
    assert records[0].exc_info is not None and records[0].exc_info[0] is RuntimeError
