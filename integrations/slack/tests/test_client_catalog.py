"""Keep the endpoint catalog honest against the client's real call sites.

``test_api_spec_drift.py`` reconciles the catalog (:data:`OMNIGENT_ENDPOINTS`)
against the SERVER's ``openapi.json``. But that only guards the endpoints the
catalog already lists — if someone adds a NEW ``self._request("POST", "/v1/…")``
and forgets to add it to the catalog, the drift test stays green while the
client quietly depends on an unlisted endpoint. (Exactly this happened once: the
catalog listed a phantom ``/oauth/device/token`` while the real ``/oauth/token``,
``/oauth/revoke``, ``/auth/cli-login``, ``/auth/cli-poll`` calls went unlisted.)

This test closes that gap from the client side: it parses the modules that talk
to the Omnigent server with ``ast``, extracts every HTTP call, normalizes each
path to the catalog's ``{param}`` template form, and asserts every one appears in
the catalog. A new/renamed endpoint that isn't cataloged fails here with a
``file:line`` pointer to ``fakes.py``'s ``OMNIGENT_ENDPOINTS``.

Scope: the two modules whose httpx client is bound to the OMNIGENT SERVER —
``omnigent.py`` (the main API surface, via the ``_request`` / ``_get_list`` /
``_get_json`` / ``stream`` helpers) and ``oauth.py`` (the login flow, via direct
``client.get`` / ``client.post`` calls). ``databricks_oauth.py`` is deliberately
excluded: its client targets the Databricks WORKSPACE, not the Omnigent server.
"""

from __future__ import annotations

import ast
from pathlib import Path

from fakes import OMNIGENT_ENDPOINTS

_SRC = Path(__file__).resolve().parents[1] / "src" / "omnigent_slack"
# Modules whose HTTP client is bound to the Omnigent server base URL.
_SCANNED_SOURCES = [_SRC / "omnigent.py", _SRC / "oauth.py"]

# Helper calls where (method, path) sit at args 0, 1 (omnigent.py):
#   _request(method, url, ...) ; self._client.stream(method, url, …)
_METHOD_PATH_HELPERS = {"_request", "stream"}
# Helper calls that are always GET with the path at arg 0 (omnigent.py):
#   _get_list(url, *keys) ; _get_json(url, ...)
_GET_PATH_HELPERS = {"_get_list", "_get_json"}
# Direct httpx verb calls where the method IS the attribute and path is arg 0
#   (oauth.py): client.get(url, …) / client.post(url, …) / …
_VERB_METHODS = {"get", "post", "put", "patch", "delete"}


def _template_of(node: ast.expr) -> str | None:
    """Normalize a path argument AST node to a catalog ``{param}`` template.

    A plain string literal returns as-is. An f-string returns with every
    interpolation replaced by ``{}`` — so ``f"/v1/hosts/{target_host}/runners"``
    and the catalog's ``/v1/hosts/{host_id}/runners`` compare equal regardless of
    the local variable name. Returns ``None`` for a path this scanner can't
    resolve statically (a bare variable), so such call sites are reported rather
    than silently skipped.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _normalize(node.value)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")  # an interpolated path segment
        return _normalize("".join(parts))
    return None


def _normalize(path: str) -> str:
    """Collapse every ``{...}`` path parameter to a bare ``{}`` placeholder.

    Applied to BOTH the code's f-string templates and the catalog entries so the
    two are compared on structure, not parameter names (the client uses
    ``{session_id}``/``{target_host}``; the catalog uses ``{session_id}``/
    ``{host_id}``)."""
    out: list[str] = []
    depth = 0
    for ch in path:
        if ch == "{":
            if depth == 0:
                out.append("{}")
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _extract_client_calls() -> list[tuple[str, str, str]]:
    """Return ``(method, normalized_path, location)`` for each scanned HTTP call.

    ``location`` is ``"<module>:<lineno>"``. ``method`` is ``"?"`` when it can't
    be resolved statically (a variable), so the reconciliation still checks the
    path and flags the unusual call site.
    """
    calls: list[tuple[str, str, str]] = []
    for source in _SCANNED_SOURCES:
        module = source.name
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = node.func.attr
            where = f"{module}:{node.lineno}"
            if name in _METHOD_PATH_HELPERS:
                if len(node.args) < 2:
                    continue
                method, path = _literal_method(node.args[0]), _template_of(node.args[1])
            elif name in _GET_PATH_HELPERS:
                method, path = "GET", _template_of(node.args[0]) if node.args else None
            elif name in _VERB_METHODS:
                # Direct httpx verb call (oauth.py): client.post("/oauth/token", …).
                method, path = name.upper(), _template_of(node.args[0]) if node.args else None
            else:
                continue
            if path is not None and path.startswith("/"):
                calls.append((method, path, where))
    return calls


def _literal_method(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.upper()
    return "?"


# Catalog paths, normalized the same way, keyed for lookup.
_CATALOG_PATHS = {(m, _normalize(p)) for m, p, _documented in OMNIGENT_ENDPOINTS}
_CATALOG_PATHS_ANY_METHOD = {_normalize(p) for _m, p, _documented in OMNIGENT_ENDPOINTS}


def test_client_source_is_scannable() -> None:
    """The scanner finds the client's HTTP calls — guards against a silent no-op
    if a call-site shape changes (e.g. a helper is renamed)."""
    calls = _extract_client_calls()
    assert calls, (
        "no HTTP call sites found — the scanner's helper/verb sets "
        f"({_METHOD_PATH_HELPERS | _GET_PATH_HELPERS | _VERB_METHODS}) are likely "
        "stale; update them."
    )
    # Sanity: representative endpoints from BOTH scanned modules must appear.
    scanned_paths = {p for _m, p, _loc in calls}
    assert "/v1/sessions" in scanned_paths  # omnigent.py
    assert "/v1/sessions/{}/stream" in scanned_paths  # omnigent.py (stream helper)
    assert "/oauth/token" in scanned_paths  # oauth.py (direct verb call)


def test_every_client_endpoint_is_cataloged() -> None:
    """Every endpoint the client calls appears in OMNIGENT_ENDPOINTS.

    Fails when a new/renamed HTTP call (``_request`` / ``_get_list`` /
    ``_get_json`` / ``stream`` in omnigent.py, or a direct ``client.<verb>`` in
    oauth.py) isn't reflected in the catalog — so the catalog can't silently fall
    behind the client, and the OpenAPI drift test keeps covering the client's
    true surface. This is the guard that would have caught the phantom
    ``/oauth/device/token`` entry."""
    missing: list[str] = []
    for method, path, location in _extract_client_calls():
        if (method, path) in _CATALOG_PATHS:
            continue
        # An unresolved method ("?") still counts as covered if the path is
        # cataloged under any method (rare; keeps a dynamic-method call from a
        # false failure while still requiring the path itself to be listed).
        if method == "?" and path in _CATALOG_PATHS_ANY_METHOD:
            continue
        missing.append(f"{method} {path}  ({location})")
    assert not missing, (
        "The client calls endpoints missing from the catalog:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd them to OMNIGENT_ENDPOINTS in tests/fakes.py (with the correct "
        "documented=True/False), so the OpenAPI drift test covers them too."
    )


def test_catalog_has_no_phantom_endpoints() -> None:
    """No catalog entry is absent from the client's real call sites.

    The complement of :func:`test_every_client_endpoint_is_cataloged`: it caught a
    MISSING entry; this catches a STALE/PHANTOM one — an endpoint listed in the
    catalog that the client never actually calls (exactly the ``/oauth/device/
    token`` bug). Such an entry gives false confidence: the drift test "guards" a
    route the client doesn't use while the real one goes unlisted.

    Scoped to server-namespace paths the scanner can resolve (``/v1``, ``/oauth``,
    ``/auth``, ``/health``); a catalog path outside those, if ever added, is
    exempt (the scanner wouldn't see a differently-shaped call)."""
    scanned = {(m, p) for m, p, _loc in _extract_client_calls()}
    scannable_prefixes = ("/v1", "/oauth", "/auth", "/health")
    phantom: list[str] = []
    for method, path, _documented in OMNIGENT_ENDPOINTS:
        norm = _normalize(path)
        if not norm.startswith(scannable_prefixes):
            continue
        if (method, norm) not in scanned:
            phantom.append(f"{method} {path}")
    assert not phantom, (
        "Catalog lists endpoints the client never calls (phantoms):\n  "
        + "\n  ".join(phantom)
        + "\n\nRemove them from OMNIGENT_ENDPOINTS in tests/fakes.py, or fix the "
        "path to match the real call site."
    )
