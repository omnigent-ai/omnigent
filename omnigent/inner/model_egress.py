"""Trusted model-egress authority resolution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from .egress.rules import is_dns_safe_host, parse_rules

UCODE_SIGNER_BINDING_ID = "databricks-ucode-v1"
_UCODE_CODEX_PATH = "/ai-gateway/codex/v1"


@dataclass(frozen=True)
class FrozenModelRoute:
    """One exact route a signer may authorize."""

    method: str
    host: str
    path: str

    def __post_init__(self) -> None:
        method = self.method.upper()
        host = self.host.lower()
        if not method.isalpha() or method == "*":
            raise ValueError("model route requires one exact HTTP method")
        if not is_dns_safe_host(host) or host.startswith("*."):
            raise ValueError("model route requires one exact DNS hostname")
        if not self.path.startswith("/") or "?" in self.path or "#" in self.path:
            raise ValueError("model route requires an absolute path without query or fragment")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "host", host)

    def matches(self, *, method: str, host: str, path: str, query: str) -> bool:
        """Return whether a request is exactly this queryless route."""
        return (
            not query
            and method.upper() == self.method
            and host.lower() == self.host
            and path == self.path
        )


@dataclass(frozen=True)
class ProviderModelBinding:
    """Provider-owned maximum signing authority."""

    endpoint: str
    maximum_routes: tuple[FrozenModelRoute, ...]


def registered_model_provider_binding(
    *,
    binding_id: str,
    trusted_session_endpoint: str,
    trusted_host: str,
) -> ProviderModelBinding:
    """Resolve a named provider's immutable maximum route."""
    if binding_id != UCODE_SIGNER_BINDING_ID:
        raise ValueError(f"unsupported model signer provider {binding_id!r}")
    host = urlsplit(trusted_host)
    endpoint = _parse_endpoint(trusted_session_endpoint)
    if (
        host.scheme != "https"
        or host.hostname is None
        or host.username is not None
        or host.password is not None
        or host.port not in (None, 443)
        or host.path not in ("", "/")
        or host.query
        or host.fragment
        or not is_dns_safe_host(host.hostname)
        or endpoint.hostname != host.hostname
        or endpoint.path.rstrip("/") != _UCODE_CODEX_PATH
    ):
        raise ValueError("signer endpoint is not the registered Databricks ucode endpoint")
    endpoint_url = f"https://{host.hostname.lower()}{_UCODE_CODEX_PATH}"
    return ProviderModelBinding(
        endpoint=endpoint_url,
        maximum_routes=(
            FrozenModelRoute(
                method="POST",
                host=host.hostname,
                path=f"{_UCODE_CODEX_PATH}/responses",
            ),
        ),
    )


def resolve_model_routes(
    *,
    provider: ProviderModelBinding,
    trusted_session_endpoint: str,
    operator_model_egress: Sequence[str],
) -> tuple[FrozenModelRoute, ...]:
    """Intersect provider, session, and operator model-egress authority."""
    provider_endpoint = _parse_endpoint(provider.endpoint)
    session_endpoint = _parse_endpoint(trusted_session_endpoint)
    operator_rules = parse_rules(operator_model_egress)

    effective = tuple(
        route
        for route in provider.maximum_routes
        if _route_is_under_endpoint(route, provider_endpoint)
        and _route_is_under_endpoint(route, session_endpoint)
        and any(rule.matches(route.method, route.host, route.path) for rule in operator_rules)
    )
    if not effective:
        raise ValueError("no effective model routes after authority intersection")
    return effective


def _parse_endpoint(raw: str) -> SplitResult:
    endpoint = urlsplit(raw)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname is None
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or endpoint.port not in (None, 443)
    ):
        raise ValueError("trusted model endpoint must be a queryless HTTPS URL on port 443")
    if not is_dns_safe_host(endpoint.hostname):
        raise ValueError("trusted model endpoint has an invalid hostname")
    if endpoint.path in ("", "/"):
        raise ValueError("trusted model endpoint requires a non-root path")
    return endpoint


def _route_is_under_endpoint(route: FrozenModelRoute, endpoint: SplitResult) -> bool:
    host = endpoint.hostname
    assert host is not None
    prefix = endpoint.path.rstrip("/")
    return route.host == host.lower() and route.path.startswith(f"{prefix}/")
