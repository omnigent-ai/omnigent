"""Which code host a ``gh`` command line names, read before ``gh`` starts.

``gh`` takes its host from several places: ``--hostname``, ``-R HOST/OWNER/REPO``, a
full-URL ``api`` endpoint, a positional ``HOST/OWNER/REPO`` or URL, and, when none of those
is given, from the checkout's remotes or ``GH_HOST``. The gate in front of every ``gh``
call reads the argv with :func:`classify`, so the host it checks is the one ``gh`` uses.

Only the commands the panel runs are known, each with the flags it uses; any other
command, flag or host-shaped argument is refused rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from omnigent.git_providers.github import _valid_hostname

# Where github.com's upload endpoints live; gh sends them with github.com's token.
_UPLOADS_HOST = "uploads.github.com"


class GhArgvRefused(ValueError):
    """A ``gh`` command line the gate can't classify, so it never runs."""


@dataclass(frozen=True)
class GhTarget:
    """What a ``gh`` command line asks for.

    :ivar command: The command words, e.g. ``("pr", "view")``.
    :ivar host: The host the argv names, lower-cased, or ``None`` when it names none.
    :ivar from_checkout: Whether ``gh`` picks the host from the checkout's remotes.
    """

    command: tuple[str, ...]
    host: str | None
    from_checkout: bool


@dataclass(frozen=True)
class _Command:
    """One command the panel runs, and the flags and arguments it may take.

    :ivar values: Flags that take a value, free text included (``-f``, ``--jq``).
    :ivar switches: Flags that take none.
    :ivar host_flags: Value flags whose value is a hostname.
    :ivar repository_flags: Value flags whose value is ``[HOST/]OWNER/REPO``.
    :ivar positional: What its one optional positional argument is, if it takes one.
    :ivar checkout: Whether, naming no repository, ``gh`` reads the checkout's remotes.
    """

    values: frozenset[str] = field(default_factory=frozenset)
    switches: frozenset[str] = field(default_factory=frozenset)
    host_flags: frozenset[str] = field(default_factory=frozenset)
    repository_flags: frozenset[str] = field(default_factory=frozenset)
    positional: str | None = None
    checkout: bool = False


_REPOSITORY = frozenset({"-R", "--repo"})
_HOSTNAME = frozenset({"-h", "--hostname"})


def _pr(*values: str, switches: tuple[str, ...] = (), positional: str | None = None) -> _Command:
    return _Command(
        values=_REPOSITORY | frozenset(values),
        switches=frozenset(switches),
        repository_flags=_REPOSITORY,
        positional=positional,
        checkout=True,
    )


_COMMANDS: dict[tuple[str, ...], _Command] = {
    ("api",): _Command(
        values=frozenset(
            {
                "--hostname",
                "-X",
                "--method",
                "-H",
                "--header",
                "-f",
                "--raw-field",
                "-F",
                "--field",
                "-q",
                "--jq",
                "--input",
                "--cache",
            }
        ),
        host_flags=frozenset({"--hostname"}),
        positional="endpoint",
    ),
    ("auth", "status"): _Command(values=_HOSTNAME | {"--json"}, host_flags=_HOSTNAME),
    ("auth", "token"): _Command(values=_HOSTNAME | {"-u", "--user"}, host_flags=_HOSTNAME),
    ("repo", "view"): _Command(
        values=frozenset({"--json"}), positional="repository", checkout=True
    ),
    ("repo", "set-default"): _Command(
        switches=frozenset({"-v", "--view"}), positional="repository_or_remote", checkout=True
    ),
    ("pr", "view"): _pr("--json", positional="pull_request"),
    ("pr", "diff"): _pr(positional="pull_request"),
    ("pr", "merge"): _pr(
        "--match-head-commit",
        switches=("--merge", "--squash", "--rebase", "--disable-auto"),
        positional="pull_request",
    ),
    ("pr", "list"): _pr(
        "--head", "-H", "--base", "-B", "--state", "-s", "--limit", "-L", "--json"
    ),
    ("pr", "close"): _pr(positional="pull_request"),
    ("pr", "reopen"): _pr(positional="pull_request"),
    ("pr", "ready"): _pr(switches=("--undo",), positional="pull_request"),
    ("label", "list"): _pr("--search", "-S", "--limit", "-L", "--json"),
}


def _hostname(value: str) -> str:
    host = value.lower()
    if not _valid_hostname(host):
        raise GhArgvRefused("A gh host flag names something that isn't a hostname")
    return host


def _url_host(value: str) -> str:
    """The host of an ``https://`` URL with no userinfo or port."""
    if not value.startswith("https://"):
        raise GhArgvRefused("A gh argument names a URL that isn't https")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
    except ValueError as exc:
        raise GhArgvRefused("A gh argument names a URL that can't be read") from exc
    if parts.netloc.lower() != host or not _valid_hostname(host):
        raise GhArgvRefused("A gh argument names a URL with userinfo, a port or no host")
    return host


def _repository_host(value: str) -> str | None:
    """The host of ``[HOST/]OWNER/REPO`` or a repository URL; ``None`` for ``OWNER/REPO``."""
    if "://" in value:
        return _url_host(value)
    parts = value.split("/")
    if any(not part or set(part) & set(":@\\ ") for part in parts):
        raise GhArgvRefused("A gh repository argument isn't OWNER/REPO or HOST/OWNER/REPO")
    if len(parts) == 2:
        return None
    if len(parts) == 3:
        return _hostname(parts[0])
    raise GhArgvRefused("A gh repository argument isn't OWNER/REPO or HOST/OWNER/REPO")


def _positional_host(kind: str, value: str) -> tuple[str | None, bool]:
    """The host a positional argument names, and whether it leaves gh to the checkout."""
    if kind == "endpoint":
        if "://" in value:
            host = _url_host(value)
            return ("github.com" if host == _UPLOADS_HOST else host), False
        if any(marker in value for marker in ("{owner}", "{repo}", "{branch}")):
            return None, True
        return None, False
    if kind == "pull_request":
        if value.isascii() and value.isdigit():
            return None, False
        if "://" in value:
            return _url_host(value), False
        raise GhArgvRefused("A gh pull request argument isn't a number or a URL")
    if (
        kind == "repository_or_remote"
        and value
        and all(character.isalnum() or character in "_-" for character in value)
    ):
        # A remote's name: gh reads the remote's URL from the checkout.
        return None, True
    return _repository_host(value), False


def classify(argv: list[str]) -> GhTarget:
    """Read which host ``argv`` (the arguments after ``gh``) sends to.

    :raises GhArgvRefused: For a command or flag the panel doesn't run, a missing flag
        value, more than one positional argument, an argument that looks like a host or a
        URL but can't be read as one, or two arguments that name different hosts.
    """
    command = next(
        (words for words in _COMMANDS if tuple(argv[: len(words)]) == words),
        None,
    )
    if command is None:
        raise GhArgvRefused("This gh command isn't one the pull request panel runs")
    spec = _COMMANDS[command]
    hosts: set[str] = set()
    named_repository = False
    from_checkout = False
    positionals: list[str] = []
    rest = argv[len(command) :]
    index = 0
    while index < len(rest):
        argument = rest[index]
        index += 1
        if argument == "--" or argument == "-":
            raise GhArgvRefused("A bare - or -- isn't a gh argument the panel uses")
        if not argument.startswith("-"):
            positionals.append(argument)
            continue
        if argument.startswith("--"):
            name, equals, value = argument.partition("=")
            attached = value if equals else None
        else:
            name, attached = argument[:2], (argument[2:] or None)
        if name in spec.switches and attached is None:
            continue
        if name not in spec.values:
            raise GhArgvRefused(f"gh {' '.join(command)} isn't run with {name}")
        if attached is None:
            if index >= len(rest):
                raise GhArgvRefused(f"gh's {name} needs a value")
            attached = rest[index]
            index += 1
        if name in spec.host_flags:
            hosts.add(_hostname(attached))
        elif name in spec.repository_flags:
            named_repository = True
            if (host := _repository_host(attached)) is not None:
                hosts.add(host)
    if len(positionals) > (1 if spec.positional else 0):
        raise GhArgvRefused(f"gh {' '.join(command)} takes no more than one argument here")
    for value in positionals:
        assert spec.positional is not None
        host, checkout = _positional_host(spec.positional, value)
        if spec.positional in {"repository", "repository_or_remote"}:
            named_repository = True
        if host is not None:
            hosts.add(host)
        from_checkout = from_checkout or checkout
    if len(hosts) > 1:
        raise GhArgvRefused("A gh command line names two different hosts")
    host = next(iter(hosts), None)
    if host is None and spec.checkout and not named_repository:
        from_checkout = True
    return GhTarget(command=command, host=host, from_checkout=from_checkout and host is None)
