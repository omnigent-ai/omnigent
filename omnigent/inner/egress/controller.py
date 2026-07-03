"""Parent-side L7 egress proxy controller.

Encapsulates the asyncio thread / MITM proxy / Unix socket /
random relay port / optional Proxy-Authorization token setup that
sits BEHIND every sandbox spec carrying ``egress_rules``.

Two callers:

1. :class:`omnigent.inner.os_env._HelperProcessClient` — sandboxed
   helper subprocess that handles ``sys_os_*`` tool calls. Uses the
   controller with ``require_auth=True`` because the helper has an
   inherited config FD it can use to receive the token out of band
   and inject it into ``HTTP_PROXY`` in-process after exec (so the
   token never lands in ``ps -E`` snapshots).

2. :class:`omnigent.inner.terminal.TerminalInstance` — interactive
   sandboxed shell run under tmux. Uses the controller with
   ``require_auth=False`` because the launcher script in tmux has
   no out-of-band channel for the token (tmux closes inherited FDs
   before exec), and embedding the token in the ``HTTP_PROXY`` env
   would leak it via ``ps -E`` on every shell child anyway. The
   relay's other defenses (random ephemeral port, bind-fails-loud,
   per-terminal scratch tmpdir for the socket, default-deny on
   private destinations) still hold.

The controller does NOT own the scratch tmpdir — callers create
and pass one in (``tmpdir``) so they can manage its lifecycle
together with their other sandbox state. The controller writes
the CA bundle into the tmpdir and creates the Unix socket inside
it, so the tmpdir must outlive :meth:`EgressProxyController.stop`.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import socket as _socket
import threading
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnigent._platform import IS_WINDOWS
from omnigent.inner.credential_proxy import CredentialRewriteRule
from omnigent.inner.egress.ca import ensure_ca, ensure_ca_bundle
from omnigent.inner.egress.proxy import EgressProxy
from omnigent.inner.egress.rules import parse_rules

_EGRESS_SOCKET_NAME = ".egress.sock"
_CA_ENV_KEYS = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "CURL_CA_BUNDLE",
    "PIP_CERT",
    # git/libcurl on macOS can ignore the generic SSL_CERT_FILE in some
    # builds; set the git-specific CA var too so ``git`` over HTTPS
    # consistently trusts the egress proxy's MITM CA.
    "GIT_SSL_CAINFO",
)


@dataclass
class EgressProxyHandle:
    """Live state for a started egress proxy.

    :param relay_port: Loopback TCP port the sandboxed relay should
        bind to (set in the policy as ``egress_relay_port``). The
        controller probed for an unused ephemeral port at start
        time; if the race is lost, ``start_relay`` aborts loudly.
    :param socket_path: Absolute path to the Unix socket in the
        scratch tmpdir that the relay bridges TCP traffic to. Set
        in the policy as ``egress_socket_path``.
    :param ca_bundle_path: Path to the MITM CA bundle copy inside the
        scratch tmpdir. Set in ``SSL_CERT_FILE`` / etc. on the
        helper / terminal env so its TLS clients trust the proxy's
        synthesized leaf certificates. This is the sandbox-writable
        copy and is used ONLY for that in-sandbox client trust — it is
        deliberately NOT the proxy's own upstream trust anchor (the
        proxy pins the host-side bundle at construction; see
        :func:`start_egress_proxy`).
    :param auth_token: Per-handle 256-bit Proxy-Authorization
        token. ``None`` when the caller asked for ``require_auth=
        False`` (terminal path); otherwise carried out of band to
        the helper via the config FD and injected in-process after
        exec. NEVER persisted on the policy or the spawn env.
    """

    relay_port: int
    socket_path: Path
    ca_bundle_path: Path
    auth_token: str | None
    _proxy: Any = field(repr=False)
    _loop: Any = field(repr=False)
    _thread: threading.Thread = field(repr=False)
    _stopped: bool = field(default=False, repr=False)

    def stop(self) -> None:
        """Stop the proxy and its event loop. Idempotent.

        The thread is joined with a short timeout — proxies hang
        in best-effort shutdown rather than leak file descriptors
        because the supervisor is exiting anyway.
        """
        if self._stopped:
            return
        self._stopped = True

        loop = self._loop
        proxy = self._proxy
        thread = self._thread

        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(proxy.stop(), loop)
            try:  # noqa: SIM105 — best-effort proxy shutdown
                future.result(timeout=3)
            except Exception:  # noqa: BLE001
                pass
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=3)


def start_egress_proxy(
    *,
    rules: Sequence[str],
    tmpdir: Path,
    allow_private_destinations: bool,
    require_auth: bool,
    credential_rewrites: Sequence[CredentialRewriteRule] | None = None,
) -> EgressProxyHandle:
    """Start the parent-side MITM egress proxy.

    :param rules: Allow-list of HTTP(S) rules as
        ``"METHODS host[:port]/path/glob"`` strings (parsed via
        :func:`omnigent.inner.egress.rules.parse_rules`).
    :param tmpdir: Caller-owned scratch directory. Used for the
        Unix socket and the local CA bundle copy. Must outlive
        the returned handle.
    :param allow_private_destinations: When ``False``, the proxy
        refuses to open upstream connections to RFC1918 / loopback /
        link-local / multicast / reserved addresses (defense vs
        DNS-rebinding to parent loopback services). Maps directly
        to :attr:`EgressProxy.block_private_destinations`.
    :param require_auth: When ``True``, generates a per-handle
        random token and configures the proxy to require it on every
        inbound request via ``Proxy-Authorization``. The token is
        returned on the handle for the caller to deliver out of
        band (NOT via the spawn env, which leaks to ``ps -E``).
        When ``False``, the proxy accepts any unauthenticated
        request — used by the terminal path where there's no
        out-of-band channel through tmux.
    :param credential_rewrites: Optional host-scoped real-credential
        rules the proxy applies for exact-host matches — swap-on-access
        injection by default, plus synthetic-placeholder swap for entries
        that opted into ``inject_env`` (secretless ``credential_proxy``
        support).
    :returns: A live :class:`EgressProxyHandle`. Caller must invoke
        :meth:`EgressProxyHandle.stop` on cleanup.
    :raises OSError: On Windows, where the L7 egress proxy (a Unix-socket
        MITM listener) is unavailable.
    """
    if IS_WINDOWS:
        raise OSError(
            "L7 egress filtering (os_env.sandbox.egress_rules) is not supported "
            "on Windows: the egress proxy relies on a Unix-domain socket. Remove "
            "the egress rules to run this agent on Windows, or run it on Linux/macOS."
        )
    parsed_rules = parse_rules(list(rules))
    ca_cert_path, ca_key_path = ensure_ca()
    bundle_path = ensure_ca_bundle(ca_cert_path)

    # Copy the CA bundle into the scratch tmpdir so it's visible
    # inside the namespace (scratch is always bind-mounted rw). This
    # copy is used ONLY for the in-sandbox ``SSL_CERT_FILE`` / etc.
    # env vars so the agent's TLS clients trust the proxy's MITM leaf
    # certs. It is NOT the proxy's upstream trust anchor — the agent
    # can write this file, so feeding it to the proxy would let the
    # agent control which upstream certs the parent trusts. The proxy
    # is given the host-only ``bundle_path`` instead (below).
    local_bundle = tmpdir / "ca-bundle.pem"
    shutil.copy2(bundle_path, local_bundle)

    socket_path = tmpdir / _EGRESS_SOCKET_NAME

    auth_token: str | None = None
    if require_auth:
        # 256 bits of urandom, ASCII-safe via ``token_urlsafe`` so
        # it slots into Basic-auth base64 without further escaping.
        auth_token = secrets.token_urlsafe(32)

    proxy = EgressProxy(
        parsed_rules,
        ca_cert_path,
        ca_key_path,
        # Pass the HOST-side immutable bundle
        # (``~/.cache/omnigent-egress/ca-bundle.pem`` from
        # ``ensure_ca_bundle`` — ``$HOME`` is never mounted into the
        # sandbox), NOT ``local_bundle`` in the agent-writable scratch
        # tmpdir. The proxy reads this once to build its upstream
        # verification context; sourcing it from a sandbox-writable
        # path would let the agent tamper with the parent's trust store.
        upstream_ca_bundle=bundle_path,
        # S2 (security): when the spec didn't explicitly opt into
        # ``egress_allow_private_destinations``, refuse to connect
        # to RFC1918 / loopback / link-local / multicast / reserved
        # addresses. See :meth:`EgressProxy._assert_destination_allowed`
        # for the full threat model.
        block_private_destinations=not allow_private_destinations,
        # S4 (security): per-handle token required on every inbound
        # request when set. Helper path uses it; terminal path skips
        # it (no out-of-band channel through tmux).
        auth_token=auth_token,
        credential_rewrites=list(credential_rewrites or []),
    )

    loop = asyncio.new_event_loop()
    started = threading.Event()

    def _run_proxy() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(proxy.start_unix(str(socket_path)))
        started.set()
        loop.run_forever()

    thread = threading.Thread(target=_run_proxy, name="egress-proxy", daemon=True)
    thread.start()
    started.wait(timeout=10)

    # Pick a random ephemeral port. Bind+close-immediately to ask
    # the kernel for a free port; a same-host attacker racing to
    # rebind has a sub-millisecond window. The relay's own bind
    # (in start_relay) fails loud if the race is lost — aborts
    # the helper / terminal launcher rather than running unprotected.
    #
    # On Linux bwrap this happens INSIDE the network namespace
    # which is empty, so the bind always succeeds and the race
    # doesn't apply. On macOS seatbelt the bind shares the host's
    # loopback so the race window matters.
    sock_probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        sock_probe.bind(("127.0.0.1", 0))
        relay_port = sock_probe.getsockname()[1]
    finally:
        sock_probe.close()

    return EgressProxyHandle(
        relay_port=relay_port,
        socket_path=socket_path,
        ca_bundle_path=local_bundle,
        auth_token=auth_token,
        _proxy=proxy,
        _loop=loop,
        _thread=thread,
    )


def apply_egress_env(
    env: MutableMapping[str, str],
    *,
    relay_port: int,
    ca_bundle_path: Path,
    auth_token: str | None,
) -> None:
    """Inject ``HTTP_PROXY`` / ``HTTPS_PROXY`` / CA env vars into ``env``.

    Sets the proxy URL (optionally with embedded Basic auth) and
    the CA bundle path for every common TLS client (requests, curl,
    node, pip, ...). Operates in place on the passed mapping so
    callers can reuse their existing env dict.

    Important: when ``auth_token`` is provided, it lands on the
    spawn env and IS visible via ``ps -E`` / ``KERN_PROCARGS2`` /
    ``/proc/<pid>/environ`` to any same-UID process. The helper
    path avoids this by passing ``auth_token=None`` here and
    re-injecting the token in-process after exec; only the terminal
    path embeds the token (currently it doesn't — terminals use
    ``require_auth=False`` so ``auth_token`` is ``None``).

    :param env: Mutable mapping to mutate (typically ``dict(os.environ)``
        for a soon-to-be-spawned child).
    :param relay_port: Port returned by :func:`start_egress_proxy`.
    :param ca_bundle_path: CA bundle path returned by
        :func:`start_egress_proxy`.
    :param auth_token: Optional Basic-auth token to embed in the
        proxy URL. Pass ``None`` to set a tokenless URL.
    """
    if auth_token is not None:
        proxy_url = f"http://omnigent:{auth_token}@127.0.0.1:{relay_port}"
    else:
        proxy_url = f"http://127.0.0.1:{relay_port}"
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["http_proxy"] = proxy_url
    env["https_proxy"] = proxy_url
    bundle_str = str(ca_bundle_path)
    for key in _CA_ENV_KEYS:
        env[key] = bundle_str


__all__ = ["EgressProxyHandle", "apply_egress_env", "start_egress_proxy"]
