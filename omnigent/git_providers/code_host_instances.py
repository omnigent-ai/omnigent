"""Code host instances a host's own config file declares.

A code host instance is a host that a provider's API serves, reached through a CLI
the host's operator names, for example a GitHub-compatible host whose CLI signs in
by its own means rather than with a stored token. Only the host's local file
declares instances. Nothing a server, a request or a synced setting sends can name
a command or its environment.

The file is TOML, read once when the host starts::

    [[instance]]
    id = "internal"
    provider = "github"
    display_name = "Internal code host"
    hosts = ["code.example.test"]
    cli = ["/opt/example/bin/gh"]
    env = ["HOME"]
    env_set = { LANG = "C.UTF-8" }
    auth = "cli"
    capabilities = ["line_counts", "linked_pr_diff", "review_threads"]

The first ``cli`` entry is an absolute path, or a bare name looked up once on the
host's ``PATH`` at load and never again. Any broken rule refuses the whole file, so
a host never runs with part of an operator's config.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import tomllib
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

from omnigent.git_providers.code_host_environment import (
    EnvironmentRuleError,
    check_environment,
    pinned_environment,
)

# The file's name in the host's config directory.
CONFIG_FILE_NAME = "code_host_instances.toml"
# Providers whose pull request facets can run through an instance's CLI.
INSTANCE_PROVIDERS = frozenset({"github"})
# Sign-in an instance's CLI handles itself: no tokens, no account switching.
CLI_AUTH = "cli"
# Hosts a provider owns by name; an instance can't claim them.
_RESERVED_HOSTS = frozenset({"github.com"})

_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_INSTANCE_KEYS = frozenset(
    {"id", "provider", "display_name", "hosts", "cli", "env", "env_set", "auth", "capabilities"}
)
_MAX_DISPLAY_NAME = 64
_MAX_ARGUMENT = 4096


class CodeHostInstanceError(ValueError):
    """The instance config is refused, or an instance can no longer run.

    Messages name the rule, the instance id and an entry's position, never a value.
    """


_LOADER_ONLY = "code host instances are built only by load_code_host_instances"


@dataclass(frozen=True, init=False, eq=False, slots=True, weakref_slot=True)
class CodeHostInstance:
    """One configured code host instance, built only by :func:`load_code_host_instances`.

    Calling the class, ``dataclasses.replace``, copying or pickling a record raises
    ``TypeError``, and a record's fields can't change once it is loaded. Records
    compare by identity.

    :ivar id: Stable id, e.g. ``"internal"``; the only instance detail logs carry.
    :ivar provider: The provider whose API it serves, e.g. ``"github"``.
    :ivar display_name: The name surfaces show for it.
    :ivar hosts: Its lower-cased host names.
    :ivar command: The CLI's absolute path and fixed arguments, resolved at load.
    :ivar passed_environment: Host environment names copied into each run.
    :ivar fixed_environment: Fixed values set in each run.
    :ivar capabilities: The most it may offer; a provider facet can offer less.
    """

    id: str
    provider: str
    display_name: str
    hosts: frozenset[str]
    command: tuple[str, ...]
    passed_environment: tuple[str, ...]
    fixed_environment: Mapping[str, str]
    capabilities: frozenset[str]

    def __init__(self, *_args: object, **_fields: object) -> NoReturn:
        raise TypeError(_LOADER_ONLY)

    def __reduce_ex__(self, _protocol: object) -> NoReturn:
        # Copies and pickles would be records the loader never issued.
        raise TypeError(_LOADER_ONLY)

    def runnable_command(self) -> list[str]:
        """Return the command to start, refusing it if its CLI has disappeared.

        The path resolved at load is checked, never looked up again.

        :raises CodeHostInstanceError: When the CLI is no longer an executable file.
        """
        if not (os.path.isfile(self.command[0]) and os.access(self.command[0], os.X_OK)):
            raise CodeHostInstanceError(
                f"instance {self.id} is refused: its CLI is no longer at the path resolved "
                "when the host started"
            )
        return list(self.command)

    def environment(self, host_environment: Mapping[str, str]) -> dict[str, str]:
        """Return the whole pinned environment for one run of this instance's CLI."""
        return pinned_environment(
            self.passed_environment, self.fixed_environment, host_environment
        )


# Every record the loader issued, held weakly; collections accept only these.
_ISSUED: weakref.WeakSet[CodeHostInstance] = weakref.WeakSet()


@dataclass(frozen=True)
class CodeHostInstances:
    """The instances a host's config declares, by provider and host.

    It also answers the provider registry's ``hosts_for``, so the hosts it lists
    resolve to their provider. It holds only records the loader issued: a copy or a
    record made some other way is refused.
    """

    instances: tuple[CodeHostInstance, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.instances, tuple) or not all(
            type(instance) is CodeHostInstance and instance in _ISSUED
            for instance in self.instances
        ):
            raise TypeError(_LOADER_ONLY)

    def hosts_for(self, provider_id: str) -> frozenset[str]:
        """Return the hosts the instances of *provider_id* declare."""
        return frozenset(
            host
            for instance in self.instances
            if instance.provider == provider_id
            for host in instance.hosts
        )

    def instance_for(self, provider_id: str, host: str | None) -> CodeHostInstance | None:
        """Return the instance of *provider_id* that serves *host*, or ``None``."""
        if not host:
            return None
        name = host.lower()
        return next(
            (
                instance
                for instance in self.instances
                if instance.provider == provider_id and name in instance.hosts
            ),
            None,
        )


NO_INSTANCES = CodeHostInstances()


def _built_instance(**values: Any) -> CodeHostInstance:
    """Return a record of *values* the loader checked, and note it as issued."""
    names = {item.name for item in dataclasses.fields(CodeHostInstance)}
    if set(values) != names:
        raise TypeError(_LOADER_ONLY)
    instance = object.__new__(CodeHostInstance)
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    _ISSUED.add(instance)
    return instance


def _refuse(message: str) -> CodeHostInstanceError:
    return CodeHostInstanceError(message)


def _host(value: object, where: str) -> str:
    if not isinstance(value, str) or value != value.strip().lower() or len(value) > 253:
        raise _refuse(f"{where} is refused: a host must be a lower-case host name")
    labels = value.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise _refuse(f"{where} is refused: a host must be a lower-case host name")
    if not labels[-1][0].isalpha():
        raise _refuse(f"{where} is refused: a host must be a lower-case host name")
    if value in _RESERVED_HOSTS:
        raise _refuse(f"{where} is refused: {value} belongs to its provider by name")
    return value


def _command(
    value: object, where: str, host_path: str, which: Callable[..., str | None]
) -> tuple[str, ...]:
    """Return the CLI's argv with its program resolved to an absolute path, once."""
    if not isinstance(value, list) or not value:
        raise _refuse(f"{where} is refused: cli must be a non-empty list of arguments")
    for index, argument in enumerate(value):
        if (
            not isinstance(argument, str)
            or not argument
            or "\0" in argument
            or len(argument) > _MAX_ARGUMENT
        ):
            raise _refuse(f"{where}[{index}] is refused: each argument must be a short string")
    program = value[0]
    if os.path.isabs(program):
        resolved = program
    elif os.sep in program or (os.altsep and os.altsep in program):
        raise _refuse(f"{where}[0] is refused: use an absolute path or a bare name on PATH")
    else:
        found = which(program, path=host_path)
        if not found or not os.path.isabs(found):
            raise _refuse(f"{where}[0] is refused: the name isn't on the host's PATH")
        resolved = found
    if not (os.path.isfile(resolved) and os.access(resolved, os.X_OK)):
        raise _refuse(f"{where}[0] is refused: it isn't an executable file")
    return (resolved, *value[1:])


def _instance(
    raw: object,
    position: int,
    *,
    known_capabilities: frozenset[str],
    host_path: str,
    which: Callable[..., str | None],
) -> CodeHostInstance:
    where = f"instance {position + 1}"
    if not isinstance(raw, dict):
        raise _refuse(f"{where} is refused: it must be a table")
    unknown = sorted(set(raw) - _INSTANCE_KEYS)
    if unknown:
        raise _refuse(f"{where} is refused: unknown field {unknown[0]}")
    instance_id = raw.get("id")
    if not isinstance(instance_id, str) or not _ID.fullmatch(instance_id):
        raise _refuse(f"{where} is refused: id must match [a-z][a-z0-9_-]*")
    where = f"instance {instance_id}"
    provider = raw.get("provider")
    if not isinstance(provider, str) or provider not in INSTANCE_PROVIDERS:
        raise _refuse(f"{where} is refused: provider must be one of {sorted(INSTANCE_PROVIDERS)}")
    display_name = raw.get("display_name")
    if (
        not isinstance(display_name, str)
        or not display_name.strip()
        or len(display_name) > _MAX_DISPLAY_NAME
        or not display_name.isprintable()
    ):
        raise _refuse(f"{where} display_name is refused: it must be a short, printable name")
    if raw.get("auth") != CLI_AUTH:
        raise _refuse(f'{where} auth is refused: it must be "{CLI_AUTH}"')
    hosts_value = raw.get("hosts")
    if not isinstance(hosts_value, list) or not hosts_value:
        raise _refuse(f"{where} hosts is refused: it must list at least one host")
    hosts: set[str] = set()
    for index, value in enumerate(hosts_value):
        host = _host(value, f"{where} hosts[{index}]")
        if host in hosts:
            raise _refuse(f"{where} hosts[{index}] is refused: it is listed twice")
        hosts.add(host)
    command = _command(raw.get("cli"), f"{where} cli", host_path, which)
    try:
        passed, fixed = check_environment(
            raw.get("env"), raw.get("env_set"), instance_id=instance_id
        )
    except EnvironmentRuleError as error:
        raise _refuse(str(error)) from None
    capabilities_value = raw.get("capabilities", [])
    if not isinstance(capabilities_value, list):
        raise _refuse(f"{where} capabilities is refused: it must be a list of names")
    capabilities: set[str] = set()
    for index, name in enumerate(capabilities_value):
        if not isinstance(name, str) or name not in known_capabilities:
            raise _refuse(f"{where} capabilities[{index}] is refused: unknown capability")
        if name in capabilities:
            raise _refuse(f"{where} capabilities[{index}] is refused: it is listed twice")
        capabilities.add(name)
    return _built_instance(
        id=instance_id,
        provider=provider,
        display_name=display_name.strip(),
        hosts=frozenset(hosts),
        command=command,
        passed_environment=passed,
        fixed_environment=MappingProxyType(dict(fixed)),
        capabilities=frozenset(capabilities),
    )


def _parse(
    text: str,
    *,
    known_capabilities: Iterable[str],
    host_path: str,
    which: Callable[..., str | None],
) -> CodeHostInstances:
    """Return the instances the host file's *text* declares; private to the loader."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise _refuse("the code host instance config is refused: it isn't valid TOML") from None
    unknown = sorted(set(document) - {"instance"})
    if unknown:
        raise _refuse(f"the code host instance config is refused: unknown field {unknown[0]}")
    raw_instances = document.get("instance", [])
    if not isinstance(raw_instances, list):
        raise _refuse("the code host instance config is refused: use [[instance]] tables")
    known = frozenset(known_capabilities)
    instances: list[CodeHostInstance] = []
    claimed: dict[str, str] = {}
    for position, raw in enumerate(raw_instances):
        instance = _instance(
            raw, position, known_capabilities=known, host_path=host_path, which=which
        )
        if any(existing.id == instance.id for existing in instances):
            raise _refuse(f"instance {instance.id} is refused: its id is used twice")
        for host in sorted(instance.hosts):
            if host in claimed:
                raise _refuse(
                    f"instance {instance.id} is refused: a host is also claimed by "
                    f"instance {claimed[host]}"
                )
            claimed[host] = instance.id
        instances.append(instance)
    return CodeHostInstances(tuple(instances))


def load_code_host_instances(
    path: Path,
    *,
    known_capabilities: Iterable[str],
    host_environment: Mapping[str, str],
    which: Callable[..., str | None] = shutil.which,
) -> CodeHostInstances:
    """Read the host's instance config at *path*, once, when the host starts.

    The only way for ordinary code to get instance records: there is no public
    constructor and no path from a frame, a request or other text, so only a file on
    the host can name a CLI. Records can't change after loading. A missing file
    declares no instances.

    :param path: The host-local config file.
    :param known_capabilities: The capability names an instance may list.
    :param host_environment: The host's environment; its ``PATH`` resolves bare names.
    :param which: Resolves a bare name on a ``PATH``; ``shutil.which`` by default.
    :raises CodeHostInstanceError: When the file can't be read or breaks a rule.
    :raises TypeError: When *path* isn't a :class:`~pathlib.Path`.
    """
    if not isinstance(path, Path):
        raise TypeError("code host instances load only from a host file path")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return NO_INSTANCES
    except (OSError, UnicodeDecodeError):
        raise _refuse("the code host instance config is refused: it can't be read") from None
    return _parse(
        text,
        known_capabilities=known_capabilities,
        host_path=host_environment.get("PATH", ""),
        which=which,
    )
