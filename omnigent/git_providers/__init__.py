"""Git provider descriptors: which forge owns a remote or pull request URL.

A descriptor recognizes its forge's hosts and URLs and names the modules that
implement the rest of the provider (connection, credential, pull request, and
policy facets). Descriptors import only the standard library, so any process
can resolve a URL cheaply and import a facet module only when it needs it.

Usage::

    from omnigent.git_providers import load_facet, resolve_pr_url

    parsed = resolve_pr_url("https://github.com/owner/repo/pull/7")
    facet = load_facet(parsed.provider, "pull_requests") if parsed else None
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

_logger = logging.getLogger(__name__)

# Built-in descriptors load before installed contributions. Each exposes ``PROVIDER``.
PROVIDER_MODULES: tuple[str, ...] = (
    "omnigent.git_providers.github",
    "omnigent.git_providers.gitlab",
    "omnigent.git_providers.azure_devops",
)
ENTRY_POINT_GROUP = "omnigent.git_providers"

_FACET_KINDS = ("connection", "credential", "pull_requests", "policy")
_SCP_REMOTE_HOST = re.compile(r"^[\w.\-]+@(?P<host>[\w.\-]+):")
_URL_SCHEMES = frozenset({"http", "https", "ssh", "git"})


@dataclass(frozen=True)
class ParsedRemote:
    """A git remote URL resolved to its provider, host, and repository path."""

    provider: str
    host: str
    repository: str


@dataclass(frozen=True)
class ParsedPullRequest:
    """A pull request URL resolved to its provider identity and canonical URL."""

    provider: str
    host: str
    repository: str
    number: int
    url: str


class Instances(Protocol):
    """Hosts configured for each provider beyond the provider's own defaults."""

    def hosts_for(self, provider_id: str) -> frozenset[str]:
        """Return the lower-cased hosts configured for *provider_id*."""
        ...


class EnvInstances:
    """Provider hosts read from ``OMNIGENT_GIT_PROVIDER_<ID>_HOSTS``.

    The value is a comma-separated host list. One configured host per provider
    is the supported case today; the list leaves room for more instances.
    """

    def hosts_for(self, provider_id: str) -> frozenset[str]:
        """Return the configured hosts for *provider_id*, stripped and lower-cased."""
        raw = os.environ.get(f"OMNIGENT_GIT_PROVIDER_{provider_id.upper()}_HOSTS", "")
        return frozenset(host for entry in raw.split(",") if (host := entry.strip().lower()))


@dataclass(frozen=True)
class FacetModules:
    """Module paths of a provider's facets, imported only by the process that uses one."""

    connection: str | None = None
    credential: str | None = None
    pull_requests: str | None = None
    policy: str | None = None


class GitProvider(Protocol):
    """A standard-library-only description of one git forge.

    The data members are read-only so a provider can declare them as plain
    class attributes or frozen dataclass fields. URL resolution logs a method
    that raises and treats that provider as not recognizing the URL.
    """

    @property
    def id(self) -> str:
        """Stable provider id stored with each reference, e.g. ``"github"``."""
        ...

    @property
    def display_name(self) -> str:
        """Human-readable name, e.g. ``"GitHub"``."""
        ...

    @property
    def default_hosts(self) -> tuple[str, ...]:
        """Lower-cased hosts the provider owns without configuration."""
        ...

    @property
    def facets(self) -> FacetModules:
        """Module paths of the provider's facets."""
        ...

    def matches_host(self, host: str, instances: Instances) -> bool:
        """Return whether *host* (lower-cased) belongs to this provider."""
        ...

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        """Parse a git remote URL on this provider, or return ``None``."""
        ...

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        """Parse a pull request URL on this provider, or return ``None``."""
        ...


def host_of(url: str) -> str | None:
    """Return the lower-cased host of an HTTP(S), SSH, git, or scp-style remote URL.

    :param url: e.g. ``"https://github.com/o/r"``, ``"ssh://git@host/o/r"``, or
        ``"git@host:o/r.git"``.
    :returns: The host, or ``None`` when *url* has no recognizable host.
    """
    candidate = url.strip()
    scp = _SCP_REMOTE_HOST.match(candidate)
    if scp is not None:
        return scp["host"].lower()
    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme not in _URL_SCHEMES:
        return None
    return host or None


# Discovery is cached for the process; provider facets remain lazy.
_lock = threading.RLock()
_providers: tuple[GitProvider, ...] | None = None
_failed_descriptors: set[str] = set()
_failed_descriptors_lock = threading.Lock()


def _load_providers() -> tuple[GitProvider, ...]:
    entries = [
        importlib.metadata.EntryPoint(name=path, value=f"{path}:PROVIDER", group=ENTRY_POINT_GROUP)
        for path in PROVIDER_MODULES
    ]
    try:
        entries.extend(
            sorted(
                importlib.metadata.entry_points(group=ENTRY_POINT_GROUP),
                key=lambda entry: (entry.name, entry.value),
            )
        )
    except Exception:  # noqa: BLE001 — installed metadata must not hide built-ins
        _logger.warning("Failed to discover git provider plugins", exc_info=True)
    loaded: dict[str, GitProvider] = {}
    for entry in entries:
        try:
            contribution = entry.load()
            descriptor = contribution() if callable(contribution) else contribution
            descriptor_id = getattr(descriptor, "id", None)
            display_name = getattr(descriptor, "display_name", None)
            if (
                not isinstance(descriptor_id, str)
                or re.fullmatch(r"[a-z][a-z0-9_-]*", descriptor_id) is None
                or not isinstance(display_name, str)
                or not display_name.strip()
                or not isinstance(getattr(descriptor, "facets", None), FacetModules)
                or not all(
                    callable(getattr(descriptor, method, None))
                    for method in ("matches_host", "parse_remote_url", "parse_pr_url")
                )
            ):
                raise ValueError("Contribution must be a git provider descriptor or factory")
            if descriptor_id in loaded:
                raise ValueError(f"Provider id {descriptor_id!r} is already defined")
            loaded[descriptor_id] = cast(GitProvider, descriptor)
        except Exception:  # noqa: BLE001 — a broken plugin must not hide other providers
            _logger.warning("Failed to load git provider %s; skipping", entry.name, exc_info=True)
    return tuple(loaded.values())


def providers() -> tuple[GitProvider, ...]:
    """Return built-ins and installed ``omnigent.git_providers`` entry points.

    Contributions expose a lightweight descriptor or a zero-argument factory.
    Duplicate IDs and broken contributions are skipped. Discovery runs once per
    process; install a plugin in the host environment and restart to pick it up.
    """
    global _providers
    with _lock:
        if _providers is None:
            _providers = _load_providers()
        return _providers


def provider(provider_id: str) -> GitProvider | None:
    """Return the provider with *provider_id*, or ``None``."""
    return next((p for p in providers() if p.id == provider_id), None)


def provider_display(provider_id: str) -> dict[str, str] | None:
    """Return the provider's labels for shared pull request surfaces."""
    descriptor = provider(provider_id)
    if descriptor is None:
        return None
    request_name = getattr(descriptor, "request_name", None)
    number_prefix = getattr(descriptor, "number_prefix", None)
    return {
        "id": descriptor.id,
        "display_name": descriptor.display_name,
        "request_name": request_name if isinstance(request_name, str) else "pull request",
        "number_prefix": number_prefix if isinstance(number_prefix, str) else "#",
    }


def reset_for_tests() -> None:
    """Forget cached discovery and failures so the next test starts cleanly."""
    global _providers
    with _lock:
        _providers = None
    with _failed_descriptors_lock:
        _failed_descriptors.clear()


def _is_module_or_parent(name: str | None, module_path: str) -> bool:
    """Return whether *name* is *module_path* or one of its parent packages."""
    return name is not None and (name == module_path or module_path.startswith(f"{name}."))


def load_facet(provider_id: str, kind: str) -> Any | None:
    """Import a provider facet and return its ``kind.upper()`` attribute.

    :param provider_id: e.g. ``"github"``.
    :param kind: One of ``connection``, ``credential``, ``pull_requests``, or
        ``policy``.
    :returns: The facet object, or ``None`` when the provider or facet is
        unset, the facet module does not exist, or it lacks the attribute.
    :raises ValueError: If *kind* is not a facet kind.
    """
    if kind not in _FACET_KINDS:
        raise ValueError(f"Unknown git provider facet kind: {kind!r}")
    descriptor = provider(provider_id)
    module_path = getattr(descriptor.facets, kind) if descriptor is not None else None
    if not isinstance(module_path, str) or not module_path:
        return None
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        # A missing dependency inside an existing facet module is a real error.
        if not _is_module_or_parent(exc.name, module_path):
            raise
        return None
    return getattr(module, kind.upper(), None)


def _log_descriptor_failure(descriptor: GitProvider, method: str) -> None:
    """Warn with a traceback the first time a provider's descriptor raises, then use debug.

    Call from an exception handler. Every URL resolution consults each descriptor, so one
    that always raises would otherwise warn each time.
    """
    with _failed_descriptors_lock:
        first = descriptor.id not in _failed_descriptors
        _failed_descriptors.add(descriptor.id)
    _logger.log(
        logging.WARNING if first else logging.DEBUG,
        "Git provider %s failed in %s; treating the URL as not recognized by it",
        descriptor.id,
        method,
        exc_info=True,
    )


def _claims(descriptor: GitProvider, host: str, instances: Instances) -> bool:
    """Return whether *descriptor* claims *host*; one that raises claims nothing."""
    try:
        return descriptor.matches_host(host, instances)
    except Exception:  # noqa: BLE001 — a broken descriptor must not stop the other providers
        _log_descriptor_failure(descriptor, "matches_host")
        return False


def _parse_remote(descriptor: GitProvider, url: str, instances: Instances) -> ParsedRemote | None:
    """Return *descriptor*'s parse of a remote URL; one that raises parses nothing."""
    try:
        parsed = descriptor.parse_remote_url(url, instances)
        if parsed is not None and (
            not isinstance(parsed, ParsedRemote)
            or parsed.provider != descriptor.id
            or not isinstance(parsed.host, str)
            or not parsed.host
            or not isinstance(parsed.repository, str)
            or not parsed.repository
        ):
            raise ValueError("Invalid remote identity returned by git provider")
        return parsed
    except Exception:  # noqa: BLE001 — a broken descriptor must not stop the other providers
        _log_descriptor_failure(descriptor, "parse_remote_url")
        return None


def _parse_pr(descriptor: GitProvider, url: str, instances: Instances) -> ParsedPullRequest | None:
    """Return *descriptor*'s parse of a pull request URL; one that raises parses nothing."""
    try:
        parsed = descriptor.parse_pr_url(url, instances)
        if parsed is not None and (
            not isinstance(parsed, ParsedPullRequest)
            or parsed.provider != descriptor.id
            or not isinstance(parsed.host, str)
            or not parsed.host
            or not isinstance(parsed.repository, str)
            or not parsed.repository
            or type(parsed.number) is not int
            or parsed.number <= 0
            or not isinstance(parsed.url, str)
            or not parsed.url
        ):
            raise ValueError("Invalid pull request identity returned by git provider")
        return parsed
    except Exception:  # noqa: BLE001 — a broken descriptor must not stop the other providers
        _log_descriptor_failure(descriptor, "parse_pr_url")
        return None


def _candidates(url: str, instances: Instances) -> Iterator[GitProvider]:
    """Yield providers that claim the URL's host, then the rest, in registration order."""
    host = host_of(url)
    remaining = list(providers())
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        authority = (
            f"{hostname}:{parsed.port}"
            if parsed.scheme in {"http", "https"} and parsed.port
            else None
        )
    except ValueError:
        authority = None
    if authority is not None:
        exact = [p for p in remaining if _claims(p, authority, instances)]
        yield from exact
        remaining = [p for p in remaining if p not in exact]
    others: list[GitProvider] = []
    for descriptor in remaining:
        if host is not None and _claims(descriptor, host, instances):
            yield descriptor
        else:
            others.append(descriptor)
    yield from others


def resolve_remote(url: str, instances: Instances | None = None) -> ParsedRemote | None:
    """Resolve a git remote URL with the first provider that parses it.

    A descriptor that raises is logged and skipped, so the other providers still resolve.

    :param url: A git remote URL, e.g. ``"git@github.com:o/r.git"``.
    :param instances: Configured provider hosts; defaults to :class:`EnvInstances`.
    :returns: The parsed remote, or ``None`` when no provider recognizes it.
    """
    instances = EnvInstances() if instances is None else instances
    for descriptor in _candidates(url, instances):
        parsed = _parse_remote(descriptor, url, instances)
        if parsed is not None:
            return parsed
    return None


def resolve_pr_url(url: str, instances: Instances | None = None) -> ParsedPullRequest | None:
    """Resolve a pull request URL with the first provider that parses it.

    A descriptor that raises is logged and skipped, so the other providers still resolve.

    :param url: A pull request URL, e.g. ``"https://github.com/o/r/pull/7"``.
    :param instances: Configured provider hosts; defaults to :class:`EnvInstances`.
    :returns: The parsed pull request, or ``None`` when no provider recognizes it.
    """
    instances = EnvInstances() if instances is None else instances
    for descriptor in _candidates(url, instances):
        parsed = _parse_pr(descriptor, url, instances)
        if parsed is not None:
            return parsed
    return None
