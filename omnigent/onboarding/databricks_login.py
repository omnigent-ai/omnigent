"""
Run ``databricks auth login`` on machines that can't open the user's browser.

The CLI waits for the OAuth redirect on a loopback port (``localhost:8020``,
or the next free port at or above it) of the machine it runs on. Over SSH the
user's browser lives on another computer, so that redirect never reaches the
listener unless the user forwarded the port themselves. On such machines this
module prints the sign-in link, lets the user paste back the address their
browser failed to load, and replays it against the listener locally.

The authorize-URL parsers are shared with the sandbox bootstrap, which drives
the same CLI output from the other side of a sandbox transport.
"""

from __future__ import annotations

import os
import queue
import re
import select
import subprocess
import sys
import threading
from typing import IO, TYPE_CHECKING
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

import click
import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

ANSI_ESCAPE_PATTERN: re.Pattern[str] = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
"""ANSI CSI escape sequences. CLI output relayed over a PTY may wrap URL lines
in color/cursor codes that must be stripped before parsing."""

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_POLL_SECONDS = 0.2
_DELIVER_TIMEOUT_SECONDS = 30.0

_PASTE_INSTRUCTIONS = """
This machine can't open a browser for you, so finish signing in by hand:
  1. Open the link above in a browser on your own computer and sign in.
  2. The browser then fails to load http://localhost:{port}/?code=... (that's expected).
  3. Copy that full address from the address bar, paste it here, and press Enter.
If port {port} is already forwarded to this machine, sign-in finishes on its own."""


def extract_oauth_url(line: str) -> str | None:
    """
    Pull a Databricks OAuth authorize URL out of one line of CLI output.

    ``databricks auth login`` prints the verification URL on its own
    line (``https://<host>/oidc/v1/authorize?...``). Output relayed over a
    PTY may carry ANSI codes and a trailing carriage return; both are
    stripped before matching.

    :param line: One line of combined stdout/stderr from the login
        process.
    :returns: The authorize URL if this line contains one, else
        ``None``.
    """
    clean = ANSI_ESCAPE_PATTERN.sub("", line).strip()
    start = clean.find("https://")
    if start == -1 or "/oidc/v1/authorize" not in clean:
        return None
    return clean[start:]


def loopback_port_from_authorize_url(url: str) -> int:
    """
    Extract the loopback callback port from an OAuth authorize URL.

    The authorize URL carries a ``redirect_uri`` query parameter such as
    ``http://localhost:8022``. The CLI binds that port dynamically (the
    first free loopback port at/above 8020), so the actual value must be
    read back from the URL.

    :param url: The authorize URL, e.g. ``"https://example.databricks."
        "com/oidc/v1/authorize?...&redirect_uri=http%3A%2F%2Flocalhost"
        "%3A8022&..."``.
    :returns: The callback port, e.g. ``8022``.
    :raises click.ClickException: If no ``redirect_uri`` with a loopback
        port can be parsed (the login URL format changed).
    """
    redirect = parse_qs(urlparse(url).query).get("redirect_uri", [None])[0]
    port = urlparse(redirect).port if redirect else None
    if port is None:
        raise click.ClickException(
            f"Could not parse an OAuth callback port from the login URL: {url}"
        )
    return port


def browser_is_remote() -> bool:
    """
    Whether the CLI's own browser launch can't reach the person signing in.

    True for an interactive SSH session, or a Linux session with no
    display, unless ``BROWSER`` names an opener: remote-dev shims (Arca's
    ``www-browser``) relay the URL to the user's computer and forward the
    callback port, so the CLI's normal flow already works there.

    :returns: ``True`` when the sign-in redirect needs the paste fallback.
    """
    browser = os.environ.get("BROWSER", "")
    if browser and browser != "none":
        return False
    # select() can't poll a Windows console handle.
    if sys.platform == "win32" or not sys.stdin.isatty():
        return False
    if _in_ssh_session():
        return True
    has_display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    return sys.platform.startswith("linux") and not has_display


def opens_browser() -> bool:
    """
    Whether ``databricks auth login`` will try to open a browser here.

    Over SSH an opener on this machine can't reach the person signing in,
    so the CLI is told to print the link instead. Elsewhere it keeps its
    usual launch, which a local relay (or a desktop) can still serve.

    :returns: ``False`` when the CLI will only print the sign-in link.
    """
    if os.environ.get("BROWSER") == "none":
        return False
    return not (browser_is_remote() and _in_ssh_session())


def _in_ssh_session() -> bool:
    """
    Whether this process runs inside an interactive SSH session.

    :returns: ``True`` when ``SSH_CONNECTION`` or ``SSH_TTY`` is set.
    """
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def run_databricks_auth_login(argv: Sequence[str]) -> int:
    """
    Run a ``databricks auth login`` command and return its exit code.

    Where the browser opens on this machine, the CLI runs attached to the
    terminal exactly as before. Where it can't (see
    :func:`browser_is_remote`), the CLI's output is relayed and the user
    can paste the failed redirect address back to finish the sign-in.

    :param argv: The full command, e.g. ``["/usr/bin/databricks", "auth",
        "login", "--host", "https://example.cloud.databricks.com",
        "--profile", "example"]``.
    :returns: The CLI's exit code.
    """
    if not browser_is_remote():
        return subprocess.run(list(argv), check=False).returncode
    return _run_with_pasted_redirect(argv)


def _run_with_pasted_redirect(argv: Sequence[str]) -> int:
    """
    Run the CLI with its output relayed, accepting a pasted redirect.

    :param argv: The full ``databricks auth login`` command.
    :returns: The CLI's exit code.
    """
    # The CLI prints the link only when it can't open a browser, so the paste
    # prompt appears exactly when the redirect has nowhere to land.
    env = dict(os.environ) if opens_browser() else {**os.environ, "BROWSER": "none"}
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env=env,
    )
    assert proc.stdout is not None
    authorize_urls: queue.Queue[str] = queue.Queue()
    relay = threading.Thread(target=_echo_output, args=(proc.stdout, authorize_urls), daemon=True)
    relay.start()
    try:
        port = _wait_for_callback_port(proc, authorize_urls)
        if port is not None:
            click.echo(_PASTE_INSTRUCTIONS.format(port=port))
            _deliver_pasted_redirects(proc, port)
        proc.wait()
    except BaseException:
        proc.terminate()
        proc.wait()
        raise
    finally:
        relay.join(timeout=5)
    return proc.returncode


def _echo_output(stream: IO[str], authorize_urls: queue.Queue[str]) -> None:
    """
    Echo the CLI's output and report each authorize URL it prints.

    :param stream: The CLI's combined stdout/stderr.
    :param authorize_urls: Receives each authorize URL after its line is
        echoed, so instructions printed in response follow the link.
    """
    for line in stream:
        click.echo(line, nl=False)
        url = extract_oauth_url(line)
        if url is not None:
            authorize_urls.put(url)


def _wait_for_callback_port(
    proc: subprocess.Popen[str], authorize_urls: queue.Queue[str]
) -> int | None:
    """
    Wait until the CLI prints its authorize URL or exits.

    :param proc: The running CLI.
    :param authorize_urls: Fed by :func:`_echo_output`.
    :returns: The callback port, or ``None`` when the CLI exited first
        (it failed before the browser step).
    """
    while True:
        try:
            return loopback_port_from_authorize_url(authorize_urls.get(timeout=_POLL_SECONDS))
        except queue.Empty:
            if proc.poll() is not None:
                return None


def _deliver_pasted_redirects(proc: subprocess.Popen[str], port: int) -> None:
    """
    Replay pasted redirect addresses against the CLI's listener until it exits.

    Reads the raw stdin descriptor so a line buffered by a text wrapper
    can't hide from ``select``. Returns when the CLI exits, whether the
    redirect came from a paste or through a port the user forwarded.

    :param proc: The running CLI.
    :param port: The CLI's loopback callback port.
    """
    stdin_fd = sys.stdin.fileno()
    pending = ""
    while proc.poll() is None:
        if "\n" not in pending:
            ready, _, _ = select.select([stdin_fd], [], [], _POLL_SECONDS)
            if not ready:
                continue
            chunk = os.read(stdin_fd, 65536)
            if not chunk:
                return
            pending += chunk.decode(errors="replace")
            continue
        line, pending = pending.split("\n", 1)
        if not line.strip():
            continue
        target = _redirect_target(line, port)
        if target is None:
            click.echo(
                f"That isn't the http://localhost:{port}/?code=... address. "
                "Paste the full address your browser tried to load."
            )
            continue
        try:
            httpx.get(target, timeout=_DELIVER_TIMEOUT_SECONDS, trust_env=False)
        except httpx.HTTPError as exc:
            click.echo(f"Couldn't hand the sign-in to the Databricks CLI: {exc}")


def _redirect_target(pasted: str, port: int) -> str | None:
    """
    Map a pasted redirect address onto the CLI's local listener.

    :param pasted: What the user pasted, e.g.
        ``"http://localhost:8020/?code=abc&state=xyz"``.
    :param port: The CLI's loopback callback port, e.g. ``8020``.
    :returns: The URL to request on this machine, or ``None`` when the
        paste isn't a loopback redirect for *port* carrying an OAuth
        ``code`` or ``error``.
    """
    parts = urlsplit(pasted.strip())
    try:
        pasted_port = parts.port
    except ValueError:
        return None
    if parts.scheme != "http" or parts.hostname not in _LOOPBACK_HOSTS or pasted_port != port:
        return None
    query = parse_qs(parts.query)
    if "code" not in query and "error" not in query:
        return None
    return urlunsplit(("http", f"localhost:{port}", parts.path or "/", parts.query, ""))
