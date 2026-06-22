"""
Cross-backend end-to-end egress parity tests.

The egress pipeline (MITM proxy + Unix socket bridge + in-namespace
TCP→Unix relay + ``HTTP_PROXY`` / ``SSL_CERT_FILE`` env injection)
is shared between ``linux_bwrap`` and ``darwin_seatbelt``. The
backends differ only in HOW they enforce network isolation: bwrap
uses ``--unshare-net`` (no route to anything except loopback +
allow-listed sockets); seatbelt emits ``(deny network*)`` with
narrow allows for the relay port and the Unix socket. The plan
calls for asserting the IDENTICAL observable behavior on both
backends so a regression in either backend's network enforcement
fails the same test.

This module covers the four contract assertions:

1. An HTTP GET matching ``egress_rules`` returns 200 through the
   proxy.
2. An HTTP GET NOT matching ``egress_rules`` returns 403 from the
   proxy.
3. A direct TCP connect that bypasses ``HTTP_PROXY`` fails with a
   connection error — the "hard enforcement" check that proves the
   network deny is real, not advisory.
4. The injected ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` /
   ``NODE_EXTRA_CA_CERTS`` / ``CURL_CA_BUNDLE`` / ``PIP_CERT`` env
   vars all point at the same CA bundle file inside scratch.

The first three assertions need real external network connectivity
(the proxy makes the upstream request). They skip cleanly when the
test host has no internet — the CI shard runs them where the
internet is reachable. The fourth assertion is observable inside
the helper without any external traffic.
"""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.inner.credential_proxy import SYNTHETIC_CREDENTIAL_PREFIX
from omnigent.inner.datamodel import (
    CredentialProxyEntry,
    CredentialProxySpec,
    CredentialSourceSpec,
    OSEnvSandboxSpec,
    OSEnvSpec,
)
from omnigent.inner.os_env import create_os_environment
from tests.unit.inner.sandbox.conftest import run_async

_PUBLIC_HOST_FOR_EGRESS = "example.com"


def _internet_reachable() -> bool:
    """
    Return whether the test host has live IPv4 internet egress.

    The egress e2e tests need a real outbound HTTP connection
    through the MITM proxy. CI shards without internet (sandboxed
    runners, air-gapped lab boxes) skip cleanly instead of erroring
    with cryptic timeouts.

    :returns: ``True`` when a TCP connect to ``example.com:443``
        succeeds within 3 seconds, ``False`` otherwise.
    """
    try:
        with socket.create_connection((_PUBLIC_HOST_FOR_EGRESS, 443), timeout=3):
            return True
    except OSError:
        return False


_skip_no_internet = pytest.mark.skipif(
    not _internet_reachable(),
    reason=f"test host cannot reach {_PUBLIC_HOST_FOR_EGRESS}:443",
)


def _python_probe_argv(probe: str) -> str:
    """
    Quote a Python -c probe for safe inclusion in a shell command.

    The helper's shell tool runs the resulting string verbatim, so
    single-quoting plus escaped embedded single quotes is the right
    transform.

    :param probe: Python source to execute inside the helper.
    :returns: A shell command string of the form
        ``<python> -c '<probe>'``.
    """
    quoted = "'" + probe.replace("'", "'\\''") + "'"
    return f"{sys.executable} -c {quoted}"


# ---------------------------------------------------------------------------
# Allow / deny / direct-bypass contract (needs internet)
# ---------------------------------------------------------------------------


@_skip_no_internet
def test_egress_allows_matching_https_get(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    An HTTPS GET matching ``egress_rules`` returns 200 through the
    proxy.

    The probe uses :mod:`urllib.request` (no extra deps) and trusts
    the injected ``SSL_CERT_FILE``. Success here means the helper's
    relay forwarded the bytes to the parent-side proxy, the proxy
    matched the rule, opened an upstream connection to
    ``example.com:443``, performed the MITM handshake, and the
    helper's TLS stack accepted the proxy's cert against the
    injected CA bundle.
    """
    # http.client (rather than urllib.request) used here because
    # ``urllib.request.urlopen`` running under CPython 3.12 hangs
    # waiting for ``_read_status`` even after the proxy returned the
    # full chunked response — reproduced both with and without the
    # sandbox in place, so it's a urllib/keep-alive interaction
    # quirk unrelated to either backend. ``http.client`` with
    # ``set_tunnel`` + an explicit ``Connection: close`` exercises
    # the same proxy + relay code path with no observed flakes.
    # First-request latency through a freshly-bound MITM proxy +
    # CloudFlare-fronted upstream can be 15+ seconds on a cold
    # connection; the test gets a single shot per spawned helper
    # (no os_env reuse) so we retry the GET up to 3 times within
    # the probe to absorb that cold-start latency. Each attempt
    # gets a 30s timeout; the total wall budget is bounded by
    # ``run_async``'s outer asyncio scheduler. A clean failure
    # (407, 403, TLS error) skips retry — only TimeoutError
    # triggers a re-attempt.
    probe = "\n".join(
        [
            "import http.client, ssl, os, sys, base64",
            "ctx = ssl.create_default_context(cafile=os.environ.get('SSL_CERT_FILE'))",
            "proxy_url = os.environ['HTTPS_PROXY']",
            "from urllib.parse import urlparse",
            "p = urlparse(proxy_url)",
            # S4: the helper's in-process env mutation rewrites
            # HTTP_PROXY/HTTPS_PROXY to include the per-helper auth
            # token as Basic-auth userinfo. ``http.client.set_tunnel``
            # does NOT auto-extract userinfo (unlike requests / httpx);
            # the probe constructs Proxy-Authorization explicitly so
            # the CONNECT survives the proxy's auth check. The proxy
            # returns 407 on missing/wrong tokens — see
            # :meth:`EgressProxy._check_proxy_auth`.
            "tunnel_headers = {}",
            "if p.username and p.password:",
            "    creds = f'{p.username}:{p.password}'.encode()",
            "    tunnel_headers['Proxy-Authorization'] = (",
            "        'Basic ' + base64.b64encode(creds).decode()",
            "    )",
            "last_err = None",
            "for attempt in range(3):",
            "    try:",
            "        conn = http.client.HTTPSConnection(",
            "            p.hostname, p.port or 80, context=ctx, timeout=30",
            "        )",
            (f"        conn.set_tunnel('{_PUBLIC_HOST_FOR_EGRESS}', 443, headers=tunnel_headers)"),
            (
                "        conn.request('GET', '/', headers={"
                f"'Host': '{_PUBLIC_HOST_FOR_EGRESS}', "
                "'Connection': 'close'})"
            ),
            "        resp = conn.getresponse()",
            "        print('STATUS', resp.status)",
            "        conn.close()",
            "        break",
            "    except TimeoutError as e:",
            "        last_err = e",
            "        try: conn.close()",
            "        except Exception: pass",
            "        continue",
            "else:",
            "    print('TIMEOUT after retries:', last_err, file=sys.stderr)",
            "    sys.exit(2)",
        ]
    )
    spec = active_sandbox_spec_factory(
        egress_rules=[f"GET {_PUBLIC_HOST_FOR_EGRESS}/**"],
    )
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=spec,
        )
    )
    try:
        result = run_async(os_env.shell(_python_probe_argv(probe)))
    finally:
        os_env.close()
    # Helper-level failures return {"error": ...} with no exit_code;
    # surface the error text instead of a KeyError.
    assert "error" not in result, result
    assert result["exit_code"] == 0, (
        f"Allowed HTTPS GET failed. stdout={result.get('stdout')!r} "
        f"stderr={result.get('stderr')!r}"
    )
    assert "STATUS 200" in result["stdout"], (
        f"Allowed GET did not return 200 through the proxy. stdout={result.get('stdout')!r}"
    )


@_skip_no_internet
def test_egress_denies_unmatched_https_get(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    An HTTPS GET NOT matching ``egress_rules`` is rejected by the
    proxy with HTTP 403.

    The MITM intercepts the CONNECT, decides the host doesn't match
    any rule, and returns ``403 Forbidden`` before forwarding any
    bytes upstream. urllib raises ``urllib.error.HTTPError`` on
    non-2xx; the probe catches it and exits 0 only when the code is
    exactly 403 — anything else (timeout, EPERM, 200) is a real
    failure of the deny contract.
    """
    # When the proxy replies 403 to ``CONNECT example.com:443``,
    # ``http.client._tunnel`` raises
    # ``OSError("Tunnel connection failed: 403 ...")``. The probe
    # catches that exception and asserts the 403 sentinel — any
    # other outcome (200 OK, EPERM, plain timeout) is a real
    # failure of the deny contract.
    probe = "\n".join(
        [
            "import http.client, ssl, os, sys, base64",
            "ctx = ssl.create_default_context(cafile=os.environ.get('SSL_CERT_FILE'))",
            "proxy_url = os.environ['HTTPS_PROXY']",
            "from urllib.parse import urlparse",
            "p = urlparse(proxy_url)",
            # S4: extract the per-helper Proxy-Authorization token from
            # the proxy URL userinfo (the helper's in-process env mutation
            # put it there). Without this the CONNECT gets 407 instead
            # of reaching the rule check that this test is asserting.
            "tunnel_headers = {}",
            "if p.username and p.password:",
            "    creds = f'{p.username}:{p.password}'.encode()",
            "    tunnel_headers['Proxy-Authorization'] = (",
            "        'Basic ' + base64.b64encode(creds).decode()",
            "    )",
            "conn = http.client.HTTPSConnection(",
            "    p.hostname, p.port or 80, context=ctx, timeout=15",
            ")",
            (f"conn.set_tunnel('{_PUBLIC_HOST_FOR_EGRESS}', 443, headers=tunnel_headers)"),
            "try:",
            (
                "    conn.request('GET', '/', headers={"
                f"'Host': '{_PUBLIC_HOST_FOR_EGRESS}', "
                "'Connection': 'close'})"
            ),
            "    resp = conn.getresponse()",
            "    print('UNEXPECTED_OK', resp.status); sys.exit(2)",
            "except OSError as e:",
            "    msg = str(e)",
            "    if '403' in msg:",
            "        print('TUNNEL_FAIL_403', msg[:200]); sys.exit(0)",
            "    print('OTHER_OSERR', type(e).__name__, msg[:200]); sys.exit(4)",
            "except Exception as e:",
            "    print('OTHER', type(e).__name__, str(e)[:200]); sys.exit(5)",
            "finally:",
            "    try: conn.close()",
            "    except Exception: pass",
        ]
    )
    spec = active_sandbox_spec_factory(
        # Allowlist is a wholly unrelated host — the deny path is
        # the default for ``example.com``.
        egress_rules=["GET unrelated.invalid/**"],
    )
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=spec,
        )
    )
    try:
        result = run_async(os_env.shell(_python_probe_argv(probe)))
    finally:
        os_env.close()
    # Helper-level failures return {"error": ...} with no exit_code;
    # surface the error text instead of a KeyError.
    assert "error" not in result, result
    assert result["exit_code"] == 0, (
        "Unmatched egress GET did not produce a 403 from the proxy. "
        f"stdout={result.get('stdout')!r} stderr={result.get('stderr')!r}"
    )
    stdout = result["stdout"]
    assert "TUNNEL_FAIL_403" in stdout, (
        f"Expected 'TUNNEL_FAIL_403' sentinel (OSError on http.client tunnel), "
        f"got stdout={stdout!r}"
    )


@_skip_no_internet
def test_egress_direct_tcp_bypass_is_blocked(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    A direct TCP connect that ignores ``HTTP_PROXY`` fails — the
    hard-enforcement check that proves the network deny is real,
    not advisory.

    Without this assertion, a misconfigured backend could appear to
    "filter egress" by setting ``HTTP_PROXY`` while leaving raw
    sockets open — agents that don't honor ``HTTP_PROXY`` (or that
    construct raw connections deliberately) would exfiltrate
    freely. On bwrap, this fails because the network namespace has
    no route. On seatbelt, this fails because ``(deny network*)``
    rejects the syscall. Either way: ``BLOCKED:*`` sentinel.

    The probe explicitly does NOT use ``urllib`` (which honors
    ``HTTP_PROXY``); it constructs a raw socket so the bypass
    attempt is unambiguous.
    """
    probe = "\n".join(
        [
            "import socket, sys",
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
            "s.settimeout(5)",
            "ok = True",
            "try:",
            f"    s.connect(('{_PUBLIC_HOST_FOR_EGRESS}', 443))",
            "    print('CONNECTED'); ok = False",
            "except (PermissionError, OSError) as e:",
            "    print(f'BLOCKED:{type(e).__name__}')",
            "finally:",
            "    s.close()",
            "sys.exit(0 if ok else 1)",
        ]
    )
    spec = active_sandbox_spec_factory(
        # Egress is active; rules don't matter for the direct-bypass
        # check because the helper never goes through the proxy.
        egress_rules=[f"GET {_PUBLIC_HOST_FOR_EGRESS}/**"],
    )
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=spec,
        )
    )
    try:
        result = run_async(os_env.shell(_python_probe_argv(probe)))
    finally:
        os_env.close()
    # Helper-level failures return {"error": ...} with no exit_code;
    # surface the error text instead of a KeyError.
    assert "error" not in result, result
    assert result["exit_code"] == 0, (
        f"Direct TCP connect to {_PUBLIC_HOST_FOR_EGRESS}:443 succeeded "
        "despite egress_rules being active — the backend's network "
        "isolation is not engaged. "
        f"stdout={result.get('stdout')!r} stderr={result.get('stderr')!r}"
    )
    assert "BLOCKED:" in result["stdout"], (
        f"Expected BLOCKED:* sentinel proving the connect was rejected, "
        f"got stdout={result.get('stdout')!r}"
    )


# ---------------------------------------------------------------------------
# CA env-var injection (no internet required)
# ---------------------------------------------------------------------------


def test_egress_injects_ca_env_vars_at_same_bundle(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    With ``egress_rules`` active, every CA-bundle env var
    (``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` /
    ``NODE_EXTRA_CA_CERTS`` / ``CURL_CA_BUNDLE`` / ``PIP_CERT``)
    points at the SAME bundle path inside scratch.

    The five names cover the dominant HTTP clients (Python urllib,
    requests, Node, curl, pip). Splitting them across different
    files would cause subtle "works in dev, breaks in CI" failures
    when one client trusts the MITM cert and another doesn't.
    """
    spec = active_sandbox_spec_factory(
        egress_rules=[f"GET {_PUBLIC_HOST_FOR_EGRESS}/**"],
    )
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=spec,
        )
    )
    try:
        result = run_async(
            os_env.shell(
                'printf "%s\\n" '
                '"SSL_CERT_FILE=$SSL_CERT_FILE" '
                '"REQUESTS_CA_BUNDLE=$REQUESTS_CA_BUNDLE" '
                '"NODE_EXTRA_CA_CERTS=$NODE_EXTRA_CA_CERTS" '
                '"CURL_CA_BUNDLE=$CURL_CA_BUNDLE" '
                '"PIP_CERT=$PIP_CERT"'
            )
        )
    finally:
        os_env.close()
    # Helper-level failures return {"error": ...} with no exit_code;
    # surface the error text instead of a KeyError.
    assert "error" not in result, result
    assert result["exit_code"] == 0
    lines = dict(line.split("=", 1) for line in result["stdout"].splitlines() if "=" in line)
    paths = {
        lines[name]
        for name in (
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
            "CURL_CA_BUNDLE",
            "PIP_CERT",
        )
    }
    assert "" not in paths, (
        f"At least one CA env var was unset inside the helper. Captured: {lines!r}"
    )
    assert len(paths) == 1, (
        "CA env vars diverge — at least one client would see a "
        f"different bundle than the others. Captured: {lines!r}"
    )
    bundle_path = paths.pop()
    assert bundle_path.endswith("ca-bundle.pem"), (
        f"Bundle path doesn't look like the MITM bundle: {bundle_path!r}"
    )


# ---------------------------------------------------------------------------
# Private-destination block (S2 fix, no internet required)
# ---------------------------------------------------------------------------


def _private_dest_probe(target_url: str) -> str:
    """
    Build a Python probe that requests *target_url* through the proxy
    and prints the proxy's response status to stdout.

    The probe captures three observable outcomes:

    - ``STATUS_<n>`` — proxy returned an HTTP response with status
      ``n`` (e.g. ``STATUS_403`` for the private-block, ``STATUS_502``
      for connect-refused when the block is opt-in'd off).
    - ``TUNNEL_FAIL_<n>`` — ``http.client._tunnel`` raised on the
      CONNECT (e.g. 403 from the proxy on the CONNECT request itself).
    - ``OTHER_<exc>:<msg>`` — anything else, surfaced verbatim so
      the test failure mode is debuggable.

    :param target_url: The fully qualified HTTPS URL to fetch through
        the proxy (host:port required when non-443).
    :returns: A multi-line Python source string ready for
        :func:`_python_probe_argv`.
    """
    from urllib.parse import urlparse

    parsed = urlparse(target_url)
    host = parsed.hostname or ""
    port = parsed.port or 443
    return "\n".join(
        [
            "import http.client, ssl, os, sys, base64",
            "ctx = ssl.create_default_context(cafile=os.environ.get('SSL_CERT_FILE'))",
            "proxy_url = os.environ['HTTPS_PROXY']",
            "from urllib.parse import urlparse",
            "p = urlparse(proxy_url)",
            # S4: forward the helper's auth token from the proxy URL into
            # the CONNECT request so we measure the destination-IP check
            # (the S2 contract this probe targets), not the auth check.
            "tunnel_headers = {}",
            "if p.username and p.password:",
            "    creds = f'{p.username}:{p.password}'.encode()",
            "    tunnel_headers['Proxy-Authorization'] = (",
            "        'Basic ' + base64.b64encode(creds).decode()",
            "    )",
            "try:",
            "    conn = http.client.HTTPSConnection(",
            "        p.hostname, p.port or 80, context=ctx, timeout=10",
            "    )",
            f"    conn.set_tunnel({host!r}, {port}, headers=tunnel_headers)",
            f"    conn.request('GET', '/', headers={{'Host': {host!r}, 'Connection': 'close'}})",
            "    resp = conn.getresponse()",
            "    print(f'STATUS_{resp.status}')",
            "    conn.close()",
            "except OSError as e:",
            "    msg = str(e)",
            "    for code in ('403', '502', '407'):",
            "        if code in msg:",
            "            print(f'TUNNEL_FAIL_{code}', msg[:120]); sys.exit(0)",
            "    print('OTHER_OSERR', type(e).__name__, msg[:120])",
            "except Exception as e:",
            "    print('OTHER', type(e).__name__, str(e)[:120])",
        ]
    )


def test_s2_egress_blocks_private_destination_by_default(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    S2: with the default ``egress_allow_private_destinations=False``,
    the proxy refuses to connect to a host that resolves to a
    loopback / RFC1918 / link-local / multicast / reserved address.

    This is the load-bearing defense against DNS-rebinding: an
    agent with a wildcard rule like ``GET *.attacker.com/**`` and a
    subdomain the attacker controls that resolves to ``127.0.0.1``
    would otherwise reach the parent's localhost services
    (debuggers, metrics endpoints, cloud-init metadata service).

    The probe targets ``127.0.0.1`` directly and asserts the proxy
    returns 403 on the CONNECT — not connection-refused (which
    would mean the block didn't fire and the proxy actually tried
    to reach loopback).
    """
    spec = active_sandbox_spec_factory(
        # Rule allows the target so we KNOW any block must come from
        # the destination check, not from rule mismatch.
        egress_rules=["GET 127.0.0.1/**"],
    )
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=spec,
        )
    )
    try:
        result = run_async(
            os_env.shell(_python_probe_argv(_private_dest_probe("https://127.0.0.1:443/")))
        )
    finally:
        os_env.close()
    # Helper-level failures return {"error": ...} with no exit_code;
    # surface the error text instead of a KeyError.
    assert "error" not in result, result
    assert result["exit_code"] == 0, (
        f"Probe failed unexpectedly. stdout={result.get('stdout')!r} "
        f"stderr={result.get('stderr')!r}"
    )
    stdout = result["stdout"]
    assert "TUNNEL_FAIL_403" in stdout, (
        "Expected TUNNEL_FAIL_403 sentinel proving the proxy refused "
        "the connection due to the private-destination block. Got "
        f"stdout={stdout!r}. If you see STATUS_* or TUNNEL_FAIL_502 "
        "the block did NOT fire and the proxy reached loopback — S2 "
        "regression."
    )


def test_s2_egress_allows_private_destination_when_opt_in(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    S2: with explicit ``egress_allow_private_destinations=True``, the
    proxy MUST attempt the upstream connection to a private address.

    This is the auditable opt-in for agents that legitimately need
    to reach intranet services. The probe sends a plain ``GET
    http://127.0.0.1:1/`` directly to the proxy (port 1 is reliably
    unbound on every host). When the opt-in is honored, the proxy
    proceeds to ``_forward_http``, tries to open the upstream TCP
    socket, and returns ``502 Bad Gateway`` on connection refused.
    When the opt-in is NOT honored (regression), the proxy returns
    ``403 Forbidden`` from the destination block before even
    attempting the connect.

    Plain HTTP avoids the MITM-cert-for-literal-IP wrinkle that
    affects the HTTPS path (Python's TLS stack rejects IP literals
    in DNS SANs), which would otherwise obscure the actual test
    signal with a cert-verify error.
    """
    probe = "\n".join(
        [
            "import http.client, os, sys, base64",
            "proxy_url = os.environ['HTTP_PROXY']",
            "from urllib.parse import urlparse",
            "p = urlparse(proxy_url)",
            # S4: forward the helper's Proxy-Authorization token onto
            # the plain-HTTP request so the proxy doesn't return 407
            # before it gets to the rule + destination checks.
            "extra_headers = {'Host': '127.0.0.1:1', 'Connection': 'close'}",
            "if p.username and p.password:",
            "    creds = f'{p.username}:{p.password}'.encode()",
            "    extra_headers['Proxy-Authorization'] = (",
            "        'Basic ' + base64.b64encode(creds).decode()",
            "    )",
            # Connect TO the proxy and send an absolute-form GET — that's
            # the wire format for plain HTTP via a forward proxy.
            "conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=10)",
            "conn.request('GET', 'http://127.0.0.1:1/', headers=extra_headers)",
            "try:",
            "    resp = conn.getresponse()",
            "    print(f'STATUS_{resp.status}')",
            "    conn.close()",
            "except Exception as e:",
            "    print('OTHER', type(e).__name__, str(e)[:120])",
            "    sys.exit(0)",
        ]
    )
    spec = active_sandbox_spec_factory(
        egress_rules=["GET 127.0.0.1/**"],
        egress_allow_private_destinations=True,
    )
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=spec,
        )
    )
    try:
        result = run_async(os_env.shell(_python_probe_argv(probe)))
    finally:
        os_env.close()
    # Helper-level failures return {"error": ...} with no exit_code;
    # surface the error text instead of a KeyError.
    assert "error" not in result, result
    assert result["exit_code"] == 0, (
        f"Probe failed unexpectedly. stdout={result.get('stdout')!r} "
        f"stderr={result.get('stderr')!r}"
    )
    stdout = result["stdout"]
    assert "STATUS_502" in stdout, (
        "Expected STATUS_502 sentinel proving the proxy ATTEMPTED an "
        "upstream connection to loopback (and the kernel refused it) "
        f"under the opt-in. Got stdout={stdout!r}. If you see "
        "STATUS_403 the opt-in was not honored — "
        "egress_allow_private_destinations is wired wrong."
    )


# ---------------------------------------------------------------------------
# S4 — cross-helper relay isolation
# ---------------------------------------------------------------------------


def test_s4_same_uid_external_process_cannot_use_helper_relay(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    S4: another process running as the same UID on the same host
    cannot use the helper's relay to make egress requests.

    Threat model: on ``darwin_seatbelt`` the helper's relay is bound
    on the host's shared loopback (macOS has no per-process network
    namespace) — any same-UID process on the box can ``lsof`` the
    port and connect. On ``linux_bwrap`` the relay is inside an
    isolated network namespace, so the test process can't actually
    reach the port at all (which is itself a passing outcome for
    this assertion: a ``ConnectionRefusedError`` proves no other
    process on the host can use it). This single test therefore
    asserts:

    - ``darwin_seatbelt``: connecting works, but every request
      lacking the correct ``Proxy-Authorization`` token is rejected
      with ``407 Proxy Authentication Required``. The auth token
      is generated per-helper and delivered via inherited pipe FD,
      so it isn't visible to any other process (verified separately
      that env / argv / kernel snapshots stay token-less).
    - ``linux_bwrap``: connection refused — the relay literally
      isn't reachable from outside the namespace.

    This is the load-bearing defense against same-UID cross-helper
    abuse on macOS. Regressing it (e.g. by dropping the auth-token
    check) means any other process the user runs can borrow this
    sandbox's egress allowlist to make outbound HTTPS requests.

    The pytest process itself plays the role of the attacker — it
    runs unsandboxed under the same UID as the helper it tries to
    abuse, which is exactly the threat model.
    """
    import socket as _socket

    spec = active_sandbox_spec_factory(egress_rules=["GET example.com/**"])
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=spec,
        )
    )
    try:
        # The helper subprocess (and the egress proxy + relay it
        # owns) is spawned lazily on first request. Send a no-op
        # to force ``_start_locked`` to run so we have a relay to
        # probe.
        warmup = run_async(os_env.shell("true"))
        assert warmup.get("exit_code") == 0, f"Helper warmup failed: {warmup!r}"

        helper = os_env._helper  # type: ignore[attr-defined]  # introspection ONLY for tests
        relay_port = helper._egress_relay_port
        assert relay_port is not None and relay_port > 0, (
            "Test setup error: the helper didn't expose a relay "
            "port — _start_egress_proxy_locked may not have run."
        )

        # First: try to connect at all. linux_bwrap: ConnectionRefused
        # (relay is in a different netns). darwin_seatbelt: connects
        # fine because loopback is shared.
        try:
            attacker_sock = _socket.create_connection(("127.0.0.1", relay_port), timeout=5)
        except (ConnectionRefusedError, OSError) as exc:
            # Acceptable outcome on linux_bwrap — the network
            # namespace means we can't even see the port. Document
            # the reason inline so a future macOS regression that
            # happens to also raise ConnectionRefusedError on
            # the wrong port number doesn't silently pass.
            if sys.platform == "darwin":
                pytest.fail(
                    "Expected darwin_seatbelt to be reachable on the "
                    f"helper's relay port {relay_port} from a same-UID "
                    f"external process (macOS shares loopback). Got "
                    f"{type(exc).__name__}: {exc}. If the relay is "
                    "now bound on a Unix socket instead, update this "
                    "test and the S4 docstring."
                )
            # On Linux, the netns barrier is the defense — return.
            return

        try:
            # Attempt 1: no Proxy-Authorization header at all.
            attacker_sock.sendall(
                b"GET http://example.com/ HTTP/1.1\r\n"
                b"Host: example.com\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            response = b""
            attacker_sock.settimeout(5)
            while True:
                chunk = attacker_sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if len(response) > 16384:
                    break
            assert b"407 Proxy Authentication Required" in response, (
                "Same-UID external process was NOT rejected with 407 "
                "when connecting to the helper's relay port without "
                "a Proxy-Authorization header. Cross-helper isolation "
                "is BROKEN — the attacker can borrow this helper's "
                f"egress rules. Got: {response[:300]!r}"
            )
        finally:
            attacker_sock.close()

        # Attempt 2: wrong token. Indistinguishable from no token.
        import base64

        attacker_sock = _socket.create_connection(("127.0.0.1", relay_port), timeout=5)
        try:
            wrong_header = b"Basic " + base64.b64encode(b"omnigent:totally-wrong-token-aa11bb22")
            attacker_sock.sendall(
                b"GET http://example.com/ HTTP/1.1\r\n"
                b"Host: example.com\r\n"
                b"Proxy-Authorization: " + wrong_header + b"\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            response = b""
            attacker_sock.settimeout(5)
            while True:
                chunk = attacker_sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if len(response) > 16384:
                    break
            assert b"407 Proxy Authentication Required" in response, (
                "Same-UID external process was NOT rejected with 407 "
                "when connecting with a WRONG token. The auth check "
                "either accepts any token or is not running at all. "
                f"Got: {response[:300]!r}"
            )
        finally:
            attacker_sock.close()
    finally:
        os_env.close()


def test_s4_two_sandboxes_cannot_borrow_each_others_proxy(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    S4: with two sandboxes running side-by-side as the same UID,
    sandbox A's relay token does NOT authenticate against sandbox
    B's proxy. This is the explicit "two helpers" version of the
    same threat model — the previous test simulated the attacker
    from the pytest process; this one proves each helper's token
    is bound to ITS OWN proxy, not transferable across instances.

    The test reaches into ``_helper`` to read both tokens — that
    introspection privilege simulates worst-case knowledge an
    attacker might gain (e.g. via a logging mistake), and proves
    the per-helper binding holds even with full token knowledge.

    Skips on linux_bwrap: the relay is inside a netns, so neither
    helper can reach the other regardless of token (which is its
    own valid defense). The token-isolation property only needs
    proving on backends without netns isolation.
    """
    import base64
    import socket as _socket

    if sys.platform != "darwin":
        pytest.skip(
            "linux_bwrap netns isolation makes cross-helper TCP "
            "unreachable — the token-isolation property is moot "
            "and tested elsewhere (proxy unit tests cover it)."
        )

    spec_a = active_sandbox_spec_factory(egress_rules=["GET example.com/**"])
    spec_b = active_sandbox_spec_factory(egress_rules=["GET example.com/**"])
    env_a = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=spec_a)
    )
    env_b = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=spec_b)
    )
    try:
        # Warm both helpers — proxy + relay start lazily on first
        # request, but the tokens are only generated then too, so
        # we need at least one request through each before
        # introspecting.
        for env in (env_a, env_b):
            warmup = run_async(env.shell("true"))
            assert warmup.get("exit_code") == 0, f"Helper warmup failed: {warmup!r}"

        helper_a = env_a._helper  # type: ignore[attr-defined]
        helper_b = env_b._helper  # type: ignore[attr-defined]

        port_a = helper_a._egress_relay_port
        port_b = helper_b._egress_relay_port
        token_a = helper_a._egress_auth_token
        token_b = helper_b._egress_auth_token

        assert port_a != port_b, "Each helper must get its own port."
        assert token_a is not None and token_b is not None
        assert token_a != token_b, (
            "Each helper must get its own token — shared tokens defeat cross-helper isolation."
        )

        # Use A's token against B's proxy.
        a_token_header = b"Basic " + base64.b64encode(f"omnigent:{token_a}".encode())
        sock = _socket.create_connection(("127.0.0.1", port_b), timeout=5)
        try:
            sock.sendall(
                b"GET http://example.com/ HTTP/1.1\r\n"
                b"Host: example.com\r\n"
                b"Proxy-Authorization: " + a_token_header + b"\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            response = b""
            sock.settimeout(5)
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if len(response) > 16384:
                    break
            assert b"407 Proxy Authentication Required" in response, (
                "Helper A's token was accepted by helper B's proxy. "
                "Cross-helper token isolation is BROKEN — a same-UID "
                "attacker who learns ANY helper's token can use it "
                "against EVERY helper's proxy. Tokens must be bound "
                f"per-EgressProxy instance. Got: {response[:300]!r}"
            )
        finally:
            sock.close()
    finally:
        env_a.close()
        env_b.close()


# ---------------------------------------------------------------------------
# Secretless credential_proxy end-to-end (local upstream, no internet)
# ---------------------------------------------------------------------------


class _CapturingUpstream:
    """A loopback HTTP server that records each ``Authorization`` header.

    The sandbox makes requests through the egress proxy to this local
    upstream, so the captured value is exactly what the proxy forwarded
    after rewriting — proving the real secret reached the service while
    the sandbox only ever held the synthetic placeholder.

    :param captured: Authorization header values seen, in arrival order.
    :param port: The bound loopback port.
    """

    def __init__(self) -> None:
        import http.server
        import threading

        captured: list[str | None] = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                captured.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args: object) -> None:
                # Silence the default stderr access log.
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.captured = captured
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop the server and join its thread."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _proxied_http_probe(target_url: str, *, auth_expr: str) -> str:
    """
    Build a Python probe that GETs *target_url* through ``HTTP_PROXY``.

    The probe constructs ``Proxy-Authorization`` from the proxy URL's
    embedded token (the helper rewrote ``HTTP_PROXY`` in-process to carry
    it) and sends ``Authorization`` built from *auth_expr*, then prints
    ``STATUS <code>``.

    :param target_url: Absolute http URL of the local upstream, e.g.
        ``"http://127.0.0.1:54321/probe"``.
    :param auth_expr: A Python expression (evaluated in the probe) that
        yields the ``Authorization`` header value, e.g.
        ``"'Bearer ' + os.environ['JIRA_TOKEN']"``.
    :returns: Python source for use with :func:`_python_probe_argv`.
    """
    return "\n".join(
        [
            "import os, base64, http.client",
            "from urllib.parse import urlparse",
            "p = urlparse(os.environ['HTTP_PROXY'])",
            "headers = {'Connection': 'close'}",
            f"headers['Authorization'] = {auth_expr}",
            "if p.username and p.password:",
            "    creds = f'{p.username}:{p.password}'.encode()",
            "    headers['Proxy-Authorization'] = 'Basic ' + base64.b64encode(creds).decode()",
            "conn = http.client.HTTPConnection(p.hostname, p.port, timeout=30)",
            f"conn.request('GET', '{target_url}', headers=headers)",
            "resp = conn.getresponse()",
            "print('STATUS', resp.status)",
            "conn.close()",
        ]
    )


def _proxied_http_probe_no_auth(target_url: str) -> str:
    """
    Build a Python probe that GETs *target_url* through ``HTTP_PROXY`` with
    no ``Authorization`` header.

    This is the swap-on-access client: it sends the request bare and
    relies on the egress proxy to attach the real credential for the
    bound host. Only ``Proxy-Authorization`` (built from the proxy URL's
    embedded token) is set, never ``Authorization``.

    :param target_url: Absolute http URL of the local upstream, e.g.
        ``"http://127.0.0.1:54321/probe"``.
    :returns: Python source for use with :func:`_python_probe_argv`.
    """
    return "\n".join(
        [
            "import os, base64, http.client",
            "from urllib.parse import urlparse",
            "p = urlparse(os.environ['HTTP_PROXY'])",
            "headers = {'Connection': 'close'}",
            "if p.username and p.password:",
            "    creds = f'{p.username}:{p.password}'.encode()",
            "    headers['Proxy-Authorization'] = 'Basic ' + base64.b64encode(creds).decode()",
            "conn = http.client.HTTPConnection(p.hostname, p.port, timeout=30)",
            f"conn.request('GET', '{target_url}', headers=headers)",
            "resp = conn.getresponse()",
            "print('STATUS', resp.status)",
            "conn.close()",
        ]
    )


def test_credential_proxy_swap_on_access_injects_basic_without_sandbox_secret(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Swap-on-access (the default): the proxy injects Basic auth on a bare request.

    This is the git_https / no-``env`` path. The binding has NO
    ``inject_env``, so nothing credential-shaped is placed in the sandbox.
    The probe sends a request with no ``Authorization`` header at all; the
    egress proxy attaches ``Basic b64(x-access-token:<real>)`` for the
    bound host on the way out. We assert both halves: the upstream
    received the real Basic credential AND the sandbox env never held the
    real secret or any ``oa_cred_*`` placeholder.
    """
    import base64

    real_secret = "gho_real_git_token_5k1"
    monkeypatch.setenv("OA_TEST_GIT_SECRET", real_secret)
    upstream = _CapturingUpstream()
    spec = active_sandbox_spec_factory(
        egress_rules=["* 127.0.0.1/**"],
        egress_allow_private_destinations=True,
        credential_proxy=CredentialProxySpec(
            entries=[
                CredentialProxyEntry(
                    host="127.0.0.1",
                    scheme="basic",
                    source=CredentialSourceSpec(kind="env", env="OA_TEST_GIT_SECRET"),
                    username="x-access-token",
                )
            ]
        ),
    )
    os_env = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=spec)
    )
    probe = _proxied_http_probe_no_auth(f"http://127.0.0.1:{upstream.port}/probe")
    try:
        result = run_async(os_env.shell(_python_probe_argv(probe)))
        # Dump every credential-shaped env var the sandbox can see.
        sandbox_env = run_async(
            os_env.shell('env | grep -E "OA_TEST_GIT_SECRET|oa_cred_" || true')
        )
    finally:
        os_env.close()
        upstream.close()

    assert result["exit_code"] == 0, (
        f"Probe failed. stdout={result.get('stdout')!r} stderr={result.get('stderr')!r}"
    )
    assert "STATUS 200" in result["stdout"], f"Did not reach upstream: {result['stdout']!r}"
    expected = "Basic " + base64.b64encode(f"x-access-token:{real_secret}".encode()).decode()
    # The proxy synthesized the Authorization header from nothing the
    # client sent — if injection regressed, captured would be [None]
    # (the bare request) instead of the real Basic credential.
    assert upstream.captured == [expected], (
        "Upstream MUST receive the proxy-injected Basic credential on a bare "
        f"request. Captured: {upstream.captured!r}."
    )
    # The sandbox never held the real secret nor a placeholder — pure
    # swap-on-access puts nothing credential-shaped in the env.
    assert real_secret not in sandbox_env["stdout"], (
        f"real secret leaked into the sandbox env: {sandbox_env['stdout']!r}"
    )
    assert SYNTHETIC_CREDENTIAL_PREFIX not in sandbox_env["stdout"], (
        f"a synthetic placeholder was injected for a swap-on-access entry: "
        f"{sandbox_env['stdout']!r}"
    )


def test_credential_proxy_https_bearer_swaps_injected_env_token(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    https_bearer: the synthetic env token is swapped for the real secret.

    Full path: parser-shaped spec -> parent resolves ``env:OA_TEST_SECRET``
    -> mints ``oa_cred_*`` -> injects it as ``JIRA_TOKEN`` in the sandbox
    -> probe sends ``Bearer <synthetic>`` -> egress proxy swaps it -> local
    upstream sees the REAL secret. We assert both halves: the upstream got
    the real token AND the sandbox env held only the synthetic.
    """
    real_secret = "real-bearer-secret-9f3a"
    monkeypatch.setenv("OA_TEST_SECRET", real_secret)
    upstream = _CapturingUpstream()
    spec = active_sandbox_spec_factory(
        egress_rules=["* 127.0.0.1/**"],
        egress_allow_private_destinations=True,
        credential_proxy=CredentialProxySpec(
            entries=[
                CredentialProxyEntry(
                    host="127.0.0.1",
                    scheme="bearer",
                    source=CredentialSourceSpec(kind="env", env="OA_TEST_SECRET"),
                    inject_env=["JIRA_TOKEN"],
                )
            ]
        ),
    )
    os_env = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=spec)
    )
    probe = _proxied_http_probe(
        f"http://127.0.0.1:{upstream.port}/probe",
        auth_expr="'Bearer ' + os.environ['JIRA_TOKEN']",
    )
    try:
        result = run_async(os_env.shell(_python_probe_argv(probe)))
        sandbox_token = run_async(os_env.shell('printf "%s" "$JIRA_TOKEN"'))
    finally:
        os_env.close()
        upstream.close()

    assert result["exit_code"] == 0, (
        f"Probe failed. stdout={result.get('stdout')!r} stderr={result.get('stderr')!r}"
    )
    assert "STATUS 200" in result["stdout"], f"Did not reach upstream: {result['stdout']!r}"
    assert upstream.captured == [f"Bearer {real_secret}"], (
        "Upstream MUST receive the real bearer secret after the proxy swap. "
        f"Captured: {upstream.captured!r}. If it shows oa_cred_*, the rewrite "
        "did not fire; if empty, the request never reached upstream."
    )
    # The sandbox itself only ever held the synthetic placeholder — the
    # real secret must never have been injected into the sandbox env.
    injected = sandbox_token["stdout"]
    assert injected.startswith(SYNTHETIC_CREDENTIAL_PREFIX), (
        f"Sandbox JIRA_TOKEN should be a synthetic placeholder, got {injected!r}"
    )
    assert real_secret not in injected


# Module guard so the helpers don't trigger lint warnings about
# unused module-level imports when no test in this file references
# them directly.
_USED_TO_QUIET_LINT = (os,)
