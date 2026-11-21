"""Where a workspace's branch pushes, and its remotes, from Git's own answers.

:func:`resolve_repository_context` reads the checked-out branch, its commit,
the push destination and the remotes through the hardened runner. Remote URLs
are reduced to host, owner and repository; anything this module can't model
exactly gives no push evidence rather than a guess. Configuration defined
outside the repository's own files is refused as UNSUPPORTED_CONFIG.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    PUSH_UNRESOLVED_PUSH_DEFAULT,
    PUSH_UNRESOLVED_PUSH_URL,
    PUSH_UNRESOLVED_REFSPEC,
    REPOSITORY_CONTEXT_PROBLEM_GIT_FAILED,
    REPOSITORY_CONTEXT_PROBLEM_GIT_UNAVAILABLE,
    REPOSITORY_CONTEXT_PROBLEM_NOT_A_REPOSITORY,
    REPOSITORY_CONTEXT_PROBLEM_REPOSITORY_CHANGING,
    REPOSITORY_CONTEXT_PROBLEM_TIMEOUT,
    REPOSITORY_CONTEXT_PROBLEM_UNSUPPORTED_CONFIG,
    PushTarget,
    PushUnresolved,
    RepositoryContextProblem,
    RepositoryRemote,
    ResolveRepositoryContextRequest,
    ResolveRepositoryContextResponse,
)
from omnigent.host.workspace_git.runner import (
    SMALL_OUTPUT_BYTES,
    Deadline,
    GitError,
    GitOutput,
    GitProblem,
    GitRepository,
    is_object_name,
)

MAXIMUM_REMOTES = 8
_CONFIG_BYTES = 256 * 1024
_RESOLVE_SECONDS = 10
_READ_ATTEMPTS = 2
_CONFIG_PATTERN = r"^(remote|branch|url|push)\."
_BRANCH_REF_PREFIX = "refs/heads/"


@dataclass(frozen=True)
class RepositoryIdentity:
    """A repository on a code host, from a remote URL."""

    host: str
    owner: str
    repo: str


def sanitize_remote_url(url: str) -> RepositoryIdentity | None:
    """The host, owner and repository a remote URL names, or ``None``.

    Other schemes, local paths, non-default ports and paths that aren't
    exactly ``owner/repo`` give ``None``. Credentials, query strings and
    fragments are dropped, never returned.
    """
    url = url.strip()
    if not url or any(
        unicodedata.category(character) == "Cc" or character == " " for character in url
    ):
        return None
    if url.startswith("https://"):
        authority, slash, path = url[len("https://") :].partition("/")
        if not slash:
            return None
        host = _host_of_authority(authority, default_port="443")
    elif url.startswith("ssh://"):
        authority, slash, path = url[len("ssh://") :].partition("/")
        if not slash:
            return None
        host = _host_of_authority(authority, default_port="22")
    elif "://" in url:
        return None
    else:
        # scp-like `[user@]host:owner/repo`; a `/` before the `:` makes it a path.
        authority, colon, path = url.partition(":")
        if not colon or "/" in authority or path.startswith("/"):
            return None
        host = _valid_host(authority.rpartition("@")[2])
    if host is None:
        return None
    path = re.split(r"[?#]", path, maxsplit=1)[0]
    if path.endswith("/"):
        path = path[:-1]
    segments = path.split("/")
    if len(segments) != 2:
        return None
    owner, repo = segments
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not (_valid_segment(owner) and _valid_segment(repo)):
        return None
    return RepositoryIdentity(host=host, owner=owner, repo=repo)


def _host_of_authority(authority: str, *, default_port: str) -> str | None:
    """The host of ``[userinfo@]host[:port]``, when the port is the scheme's default."""
    host_port = authority.rpartition("@")[2]
    host, colon, port = host_port.rpartition(":")
    if colon:
        if port != default_port:
            return None
        host_port = host
    return _valid_host(host_port)


def _valid_host(host: str) -> str | None:
    valid = (
        bool(host)
        and len(host) <= 253
        and re.fullmatch(r"[A-Za-z0-9.-]+", host) is not None
        and host[0] not in "-."
        and not host.endswith("-")
    )
    return host.rstrip(".").lower() if valid else None


def _valid_segment(segment: str) -> bool:
    return (
        0 < len(segment) <= 100
        and segment not in (".", "..")
        and re.fullmatch(r"[A-Za-z0-9_.-]+", segment) is not None
    )


@dataclass(frozen=True)
class ConfigEntry:
    """One configuration entry; the section and variable are lowercase."""

    origin: str
    section: str
    subsection: str | None
    variable: str
    value: str | None


class ConfigSnapshot:
    """The ``remote.*``, ``branch.*``, ``url.*`` and ``push.*`` configuration, read once."""

    def __init__(self, entries: list[ConfigEntry]) -> None:
        self._entries = entries

    @classmethod
    def parse(cls, output: bytes) -> ConfigSnapshot | None:
        """Parse ``config -z --show-origin`` output: ``origin\\0key[\\nvalue]\\0`` records."""
        try:
            text = output.decode("utf-8")
        except UnicodeDecodeError:
            return None
        fields = iter(text.split("\0"))
        entries: list[ConfigEntry] = []
        for origin in fields:
            if not origin:
                break
            record = next(fields, None)
            if record is None:
                return None
            key, newline, value = record.partition("\n")
            section, dot, rest = key.partition(".")
            if not dot:
                return None
            subsection, subsection_dot, variable = rest.rpartition(".")
            if not subsection_dot:
                subsection, variable = None, rest
            entries.append(
                ConfigEntry(
                    origin=origin,
                    section=section.lower(),
                    subsection=subsection,
                    variable=variable.lower(),
                    value=value if newline else None,
                )
            )
        return cls(entries)

    def values(self, section: str, subsection: str | None, variable: str) -> list[str]:
        return [
            entry.value
            for entry in self._entries
            if entry.section == section
            and entry.subsection == subsection
            and entry.variable == variable
            and entry.value is not None
        ]

    def last(self, section: str, subsection: str | None, variable: str) -> str | None:
        """The last value, which is the one Git uses for single-valued keys."""
        values = self.values(section, subsection, variable)
        return values[-1] if values else None

    def remote_names(self) -> list[str]:
        """Remote names with a URL, in configuration order."""
        names: list[str] = []
        for entry in self._entries:
            if (
                entry.section == "remote"
                and entry.variable == "url"
                and entry.subsection is not None
                and entry.subsection not in names
            ):
                names.append(entry.subsection)
        return names

    def is_deferred(self, repository_files: Sequence[str], working_directory: Path) -> bool:
        """Whether this configuration is outside what this module models exactly.

        Any URL rewrite, or a ``remote.<name>.*``/``branch.<name>.*`` key from a file
        other than the repository's own. Keys without a name, such as
        ``remote.pushDefault`` or ``branch.sort``, may come from anywhere: they
        define no remote or branch.
        """
        for entry in self._entries:
            if entry.section == "url" and entry.variable in ("insteadof", "pushinsteadof"):
                return True
            if entry.section in ("remote", "branch") and entry.subsection is None:
                continue
            if entry.section in ("remote", "branch") and not _origin_is_one_of(
                entry.origin, repository_files, working_directory
            ):
                return True
        return False


def _origin_is_one_of(origin: str, files: Sequence[str], working_directory: Path) -> bool:
    if not origin.startswith("file:"):
        return False
    path = Path(origin[len("file:") :])
    if not path.is_absolute():
        path = working_directory / path
    return os.path.realpath(path) in files


@dataclass(frozen=True)
class _Refspec:
    negative: bool
    source: str
    destination: str | None


def _parse_refspec(text: str) -> _Refspec:
    if text.startswith("+"):
        text = text[1:]
    if text.startswith("^"):
        return _Refspec(negative=True, source=text[1:], destination=None)
    source, colon, destination = text.partition(":")
    return _Refspec(negative=False, source=source, destination=destination if colon else None)


def _match_pattern(pattern: str, name: str) -> str | None:
    """The text a ``*`` in ``pattern`` stood for in ``name``; ``""`` for a literal match."""
    prefix, star, suffix = pattern.partition("*")
    if not star:
        return "" if pattern == name else None
    if "*" in suffix or not name.startswith(prefix):
        return None
    rest = name[len(prefix) :]
    if not rest.endswith(suffix):
        return None
    return rest[: len(rest) - len(suffix)] or None


class _Unmodeled(Exception):
    """A refspec this module can't map exactly."""


def _push_refspec_destination(refspecs: Sequence[str], branch: str) -> str | None:
    """Where the push refspecs send ``refs/heads/<branch>``; ``None`` when none pushes it.

    Only full ``refs/heads/`` sources are modeled: Git reports no push target for
    short or ``HEAD`` sources, so any one of them withholds evidence.
    """
    branch_ref = f"{_BRANCH_REF_PREFIX}{branch}"
    found: str | None = None
    for text in refspecs:
        refspec = _parse_refspec(text)
        if refspec.negative or not refspec.source.startswith("refs/"):
            raise _Unmodeled
        middle = _match_pattern(refspec.source, branch_ref)
        if middle is None:
            continue
        if refspec.destination is None:
            destination = branch_ref
        elif refspec.destination.startswith(_BRANCH_REF_PREFIX):
            destination = refspec.destination.replace("*", middle, 1)
        else:
            raise _Unmodeled
        if found is not None:
            raise _Unmodeled
        found = destination
    return found


def _fetch_tracking_ref(refspecs: Sequence[str], remote_branch_ref: str) -> str | None:
    """The local tracking ref the fetch refspecs give a remote branch, if one maps it."""
    found: str | None = None
    for text in refspecs:
        refspec = _parse_refspec(text)
        if refspec.negative:
            raise _Unmodeled
        if not refspec.destination:
            continue
        middle = _match_pattern(refspec.source, remote_branch_ref)
        if middle is None:
            continue
        if found is not None:
            raise _Unmodeled
        found = refspec.destination.replace("*", middle, 1)
    return found


@dataclass(frozen=True)
class GitPushFields:
    """What Git itself reports for the branch's push."""

    push: str | None = None
    push_remote: str | None = None
    upstream: str | None = None

    @classmethod
    def parse(cls, output: bytes) -> GitPushFields:
        """Parse ``%(push)``, ``%(push:remotename)`` and ``%(upstream)``, NUL-separated."""
        fields = [*output.decode("utf-8", "replace").rstrip("\n").split("\0"), "", "", ""]
        push, push_remote, upstream = (field or None for field in fields[:3])
        return cls(push=push, push_remote=push_remote, upstream=upstream)


@dataclass(frozen=True)
class PushResolution:
    """A push destination before its tracking ref's commit is read."""

    remote_name: str
    branch: str
    identity: RepositoryIdentity
    tracking_ref: str | None
    # Whether the push repository is the one the remote fetches from.
    fetches_from_push_repository: bool


class _PushWithheld(Exception):
    """Git's push target can't be modeled; ``reason`` is the PushUnresolved value."""

    def __init__(self, reason: PushUnresolved) -> None:
        super().__init__(reason)
        self.reason = reason


def _coalesce(*candidates: str | None, default: str) -> str:
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return default


def _resolve_push(
    config: ConfigSnapshot, branch: str, git: GitPushFields
) -> PushResolution | None:
    """Where ``branch`` pushes by Git's documented rules.

    ``None`` means it doesn't push, or does so in a way this module doesn't model.
    Raises :class:`_PushWithheld` when the destination can't be pinned down.
    """
    remotes = config.remote_names()
    sole_remote = remotes[0] if len(remotes) == 1 else None
    branch_remote = config.last("branch", branch, "remote")
    fetch_remote = _coalesce(branch_remote, sole_remote, default="origin")
    push_remote = _coalesce(
        config.last("branch", branch, "pushremote"),
        config.last("remote", None, "pushdefault"),
        default=fetch_remote,
    )
    if push_remote not in remotes:
        return None
    if git.push_remote is not None and git.push_remote != push_remote:
        raise _PushWithheld(PUSH_UNRESOLVED_REFSPEC)

    push_refspecs = config.values("remote", push_remote, "push")
    if push_refspecs:
        try:
            destination_ref = _push_refspec_destination(push_refspecs, branch)
        except _Unmodeled:
            raise _PushWithheld(PUSH_UNRESOLVED_REFSPEC) from None
        if destination_ref is None:
            return None
    else:
        destination_ref = _default_push_destination(config, branch, push_remote, fetch_remote)
        if destination_ref is None:
            return None
    if not destination_ref.startswith(_BRANCH_REF_PREFIX):
        raise _PushWithheld(PUSH_UNRESOLVED_REFSPEC)
    destination = destination_ref[len(_BRANCH_REF_PREFIX) :]

    # Git maps the destination to a local ref through the fetch refspecs; ours must
    # agree whenever Git reports one.
    try:
        tracking_ref = _fetch_tracking_ref(
            config.values("remote", push_remote, "fetch"), destination_ref
        )
    except _Unmodeled:
        raise _PushWithheld(PUSH_UNRESOLVED_REFSPEC) from None
    if git.push is not None and tracking_ref != git.push:
        raise _PushWithheld(PUSH_UNRESOLVED_REFSPEC)

    push_urls = config.values("remote", push_remote, "pushurl") or config.values(
        "remote", push_remote, "url"
    )
    identity = _single_identity(push_urls)
    if identity is None:
        raise _PushWithheld(PUSH_UNRESOLVED_PUSH_URL)
    fetches_from_push_repository = (
        _single_identity(config.values("remote", push_remote, "url")) == identity
    )
    return PushResolution(
        remote_name=push_remote,
        branch=destination,
        identity=identity,
        tracking_ref=tracking_ref,
        fetches_from_push_repository=fetches_from_push_repository,
    )


def _default_push_destination(
    config: ConfigSnapshot, branch: str, push_remote: str, fetch_remote: str
) -> str | None:
    """Where ``push.default`` sends ``branch`` when no push refspec does."""
    branch_ref = f"{_BRANCH_REF_PREFIX}{branch}"
    branch_remote = config.last("branch", branch, "remote")
    merges = config.values("branch", branch, "merge")
    single_upstream = merges[0] if len(merges) == 1 else None
    triangular = push_remote != fetch_remote
    mode = _coalesce(config.last("push", None, "default"), default="simple").lower()
    auto_setup = (config.last("push", None, "autosetupremote") or "").lower() in {
        "true",
        "yes",
        "on",
        "1",
    }
    if auto_setup and not merges:
        return None
    if mode == "current" or (mode == "simple" and triangular):
        return branch_ref
    if mode == "simple":
        # Central simple pushes need an upstream of the same name on this remote.
        return (
            branch_ref if single_upstream == branch_ref and branch_remote == push_remote else None
        )
    if mode in ("upstream", "tracking"):
        if (
            single_upstream is not None
            and branch_remote == push_remote
            and single_upstream.startswith(_BRANCH_REF_PREFIX)
        ):
            return single_upstream
        return None
    if mode == "matching":
        raise _PushWithheld(PUSH_UNRESOLVED_PUSH_DEFAULT)
    return None


def _single_identity(urls: Sequence[str]) -> RepositoryIdentity | None:
    """The one repository every URL names, or ``None`` when they differ or any is unsupported."""
    if not urls:
        return None
    first = sanitize_remote_url(urls[0])
    if first is None:
        return None
    if any(sanitize_remote_url(url) != first for url in urls[1:]):
        return None
    return first


def _sanitized_remotes(config: ConfigSnapshot) -> tuple[list[RepositoryRemote], int, bool]:
    """The remotes to report, the unsupported count, and whether there were too many."""
    names = config.remote_names()
    truncated = len(names) > MAXIMUM_REMOTES
    remotes: list[RepositoryRemote] = []
    unsupported = 0
    for name in names[:MAXIMUM_REMOTES]:
        urls = config.values("remote", name, "url")
        # A remote with several fetch URLs fetches only the first; that's ambiguous here.
        identity = sanitize_remote_url(urls[0]) if len(urls) == 1 else None
        if identity is None:
            unsupported += 1
        else:
            remotes.append(
                RepositoryRemote(
                    name=name,
                    host=identity.host,
                    owner=identity.owner,
                    repo=identity.repo,
                )
            )
    return remotes, unsupported, truncated


@dataclass(frozen=True)
class _Observation:
    """What one read saw; two reads that differ mean the repository changed."""

    config: bytes
    head_ref: str | None
    head_sha: str | None
    push_fields: GitPushFields | None


class _ContextProblem(Exception):
    """A problem that ends the read with a typed answer."""

    def __init__(self, problem: RepositoryContextProblem) -> None:
        super().__init__(problem)
        self.problem = problem


def _problem_of_git_error(error: GitError) -> RepositoryContextProblem:
    # Git's own error text is never surfaced; only the category is.
    if error.problem is GitProblem.TIMEOUT:
        return REPOSITORY_CONTEXT_PROBLEM_TIMEOUT
    if error.problem is GitProblem.UNAVAILABLE:
        return REPOSITORY_CONTEXT_PROBLEM_GIT_UNAVAILABLE
    return REPOSITORY_CONTEXT_PROBLEM_GIT_FAILED


def _first_line(output: GitOutput | None) -> str | None:
    if output is None:
        return None
    lines = output.stdout.decode("utf-8", "replace").splitlines()
    line = lines[0].strip() if lines else ""
    return line or None


def _object_name_or_none(text: str | None) -> str | None:
    return text if text is not None and is_object_name(text) else None


async def _run_allowing_absent(
    repository: GitRepository, arguments: Sequence[str], deadline: Deadline, stdout_bytes: int
) -> GitOutput | None:
    """Run a command whose "nothing there" answer is exit status 1."""
    output = await repository.run_unchecked(
        arguments, deadline=deadline, stdout_bytes=stdout_bytes
    )
    if output.success:
        return output
    if output.returncode == 1:
        return None
    raise GitError(GitProblem.FAILED)


async def _observe(repository: GitRepository, deadline: Deadline) -> _Observation:
    config_output = await _run_allowing_absent(
        repository,
        ["config", "-z", "--show-origin", "--get-regexp", _CONFIG_PATTERN],
        deadline,
        _CONFIG_BYTES,
    )
    head_ref = _first_line(
        await _run_allowing_absent(
            repository, ["symbolic-ref", "-q", "HEAD"], deadline, SMALL_OUTPUT_BYTES
        )
    )
    head_sha = _object_name_or_none(
        _first_line(
            await _run_allowing_absent(
                repository,
                ["rev-parse", "--verify", "-q", "--end-of-options", "HEAD^{commit}"],
                deadline,
                SMALL_OUTPUT_BYTES,
            )
        )
    )
    push_fields = None
    if head_ref is not None and head_ref.startswith(_BRANCH_REF_PREFIX):
        push_output = await repository.run(
            [
                "for-each-ref",
                "--format=%(push)%00%(push:remotename)%00%(upstream)",
                "--",
                head_ref,
            ],
            deadline=deadline,
            stdout_bytes=SMALL_OUTPUT_BYTES,
        )
        push_fields = GitPushFields.parse(push_output)
    return _Observation(
        config=config_output.stdout if config_output is not None else b"",
        head_ref=head_ref,
        head_sha=head_sha,
        push_fields=push_fields,
    )


def _repository_files(repository: GitRepository) -> list[str]:
    """The files whose ``remote.*`` and ``branch.*`` keys this module trusts."""
    return [
        os.path.realpath(repository.common_directory / "config"),
        os.path.realpath(repository.git_directory / "config.worktree"),
    ]


def _context_from(
    repository: GitRepository, observation: _Observation
) -> tuple[ResolveRepositoryContextResponse, PushResolution | None]:
    """The response from one observation, without its tracking commit."""
    config = ConfigSnapshot.parse(observation.config)
    if config is None:
        raise _ContextProblem(REPOSITORY_CONTEXT_PROBLEM_GIT_FAILED)
    if config.is_deferred(_repository_files(repository), repository.working_directory):
        raise _ContextProblem(REPOSITORY_CONTEXT_PROBLEM_UNSUPPORTED_CONFIG)
    branch = (
        observation.head_ref[len(_BRANCH_REF_PREFIX) :]
        if observation.head_ref is not None and observation.head_ref.startswith(_BRANCH_REF_PREFIX)
        else None
    )
    remotes, unsupported_remote_count, truncated_remotes = _sanitized_remotes(config)
    response = ResolveRepositoryContextResponse(
        branch=branch or "",
        head_sha=observation.head_sha or "",
        remotes=remotes,
        unsupported_remote_count=unsupported_remote_count,
        truncated_remotes=truncated_remotes,
    )
    resolution = None
    if branch is not None and observation.push_fields is not None:
        response.upstream_ref = observation.push_fields.upstream or ""
        try:
            resolution = _resolve_push(config, branch, observation.push_fields)
        except _PushWithheld as withheld:
            response.push_unresolved = withheld.reason
    return response, resolution


async def _resolve(root: Path, deadline: Deadline) -> ResolveRepositoryContextResponse:
    repository = await GitRepository.discover(root, deadline)
    if repository is None:
        raise _ContextProblem(REPOSITORY_CONTEXT_PROBLEM_NOT_A_REPOSITORY)
    # A read during which the repository changed is retried once, then declined.
    for _ in range(_READ_ATTEMPTS):
        before = await _observe(repository, deadline)
        response, resolution = _context_from(repository, before)
        tracking_sha = None
        if resolution is not None and resolution.tracking_ref is not None:
            tracking_output = await _run_allowing_absent(
                repository,
                [
                    "rev-parse",
                    "--verify",
                    "-q",
                    "--end-of-options",
                    f"{resolution.tracking_ref}^{{commit}}",
                ],
                deadline,
                SMALL_OUTPUT_BYTES,
            )
            tracking_sha = _object_name_or_none(_first_line(tracking_output))
        after = await _observe(repository, deadline)
        if after != before:
            continue
        if resolution is not None:
            response.push.CopyFrom(
                PushTarget(
                    remote_name=resolution.remote_name,
                    branch=resolution.branch,
                    host=resolution.identity.host,
                    owner=resolution.identity.owner,
                    repo=resolution.identity.repo,
                    # The tracking ref describes the fetch side, which may be another repository.
                    tracking_sha=(tracking_sha or "")
                    if resolution.fetches_from_push_repository
                    else "",
                )
            )
        return response
    raise _ContextProblem(REPOSITORY_CONTEXT_PROBLEM_REPOSITORY_CHANGING)


async def resolve_repository_context(
    root: Path, request: ResolveRepositoryContextRequest
) -> ResolveRepositoryContextResponse:
    """Read the repository context of the repository containing ``root``.

    One deadline covers discovery and every Git command. Problems come back as
    the response's ``problem``, never as an exception, and carry no Git output.
    """
    del request
    deadline = Deadline(_RESOLVE_SECONDS)
    try:
        return await _resolve(root, deadline)
    except _ContextProblem as problem:
        return ResolveRepositoryContextResponse(problem=problem.problem)
    except GitError as error:
        return ResolveRepositoryContextResponse(problem=_problem_of_git_error(error))
