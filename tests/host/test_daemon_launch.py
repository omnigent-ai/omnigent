"""Tests for the CLI-side daemon-launch polling helpers.

Covers ``omnigent.host.daemon_launch``: the online-wait loops must poll
through *transient* transport errors (connection refused while the local
server is still binding, a dropped keepalive) instead of crashing on the
first one, and must surface the last transport error when the deadline
expires. A regression here reproduces the CI failure where a single
refused status poll killed ``omnigent run`` with a bare
``httpx.ConnectError`` even though the deadline had 50+ seconds left.
"""

from __future__ import annotations

import click
import httpx
import pytest

from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER
from omnigent.cli_diagnostics import suppresses_recovery_hint
from omnigent.host import daemon_launch
from omnigent.host.daemon_launch import (
    open_daemon_client,
    runner_is_online,
    wait_for_host_online,
    wait_for_runner_online,
)
from omnigent.process_logging import process_log_dir_reference


@pytest.fixture(autouse=True)
def _no_ambient_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the machine's own host identity and runner slice-key env.

    ``databricks_request_headers`` (reached via ``open_daemon_client`` with no
    explicit host) falls back to the CLI's own host_id, so on a machine that IS
    a host (a persisted ``~/.omnigent/config.yaml`` ``host:`` section) the
    "no slice key" assertions would pick up that ambient identity.
    """
    monkeypatch.setattr(
        "omnigent.host.identity.load_host_identity_if_present",
        lambda *a, **k: None,
    )
    monkeypatch.delenv("OMNIGENT_RUNNER_SLICE_KEY", raising=False)


class _FlakyThenOnline:
    """MockTransport handler that refuses N connections, then reports online.

    :param failures: Number of initial requests that raise
        ``httpx.ConnectError`` before the endpoint starts answering.
    :param body: JSON body returned once "online", e.g.
        ``{"online": True}`` for the runner status endpoint or
        ``{"status": "online"}`` for the host endpoint.
    """

    def __init__(self, failures: int, body: dict[str, object]) -> None:
        self.failures = failures
        self.body = body
        self.requests_seen = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """
        Handle one mock-transport request.

        :param request: The outgoing request the client built.
        :returns: A 200 response with the configured body once the
            failure budget is exhausted.
        :raises httpx.ConnectError: For the first ``failures`` requests.
        """
        self.requests_seen += 1
        if self.requests_seen <= self.failures:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=self.body)


class _AlwaysRefuses:
    """MockTransport handler that refuses every connection."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """
        Refuse the connection.

        :param request: The outgoing request the client built.
        :raises httpx.ConnectError: Always.
        """
        raise httpx.ConnectError("connection refused", request=request)


class _AlwaysAnswers:
    """MockTransport handler that answers every request with one fixed response.

    :param status_code: HTTP status to answer, e.g. ``302``.
    :param headers: Response headers, e.g. a sign-in ``location``.
    :param body: Optional JSON body, e.g. ``{"status": "offline"}``, or a
        plain-text body, e.g. ``"Invalid Token"``.
    """

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        body: dict[str, object] | str | None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """
        Answer with the configured response.

        :param request: The outgoing request the client built.
        :returns: The fixed response.
        """
        if isinstance(self.body, str):
            return httpx.Response(self.status_code, headers=self.headers, text=self.body)
        return httpx.Response(self.status_code, headers=self.headers, json=self.body)


class _FirstThenRest:
    """MockTransport handler whose first request and later requests differ.

    :param first_status: HTTP status for the first request, or ``None`` to
        refuse the connection, e.g. ``401``.
    :param rest_status: HTTP status for every later request, or ``None`` to
        refuse those connections.
    """

    def __init__(self, first_status: int | None, rest_status: int | None) -> None:
        self.first_status = first_status
        self.rest_status = rest_status
        self.requests_seen = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """
        Answer or refuse according to the request's position.

        :param request: The outgoing request the client built.
        :returns: A bodiless response with the configured status.
        :raises httpx.ConnectError: When the configured status is ``None``.
        """
        self.requests_seen += 1
        status = self.first_status if self.requests_seen == 1 else self.rest_status
        if status is None:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(status)


class _HtmlThenOnline:
    """MockTransport handler that answers the SPA HTML fallback N times, then JSON-online.

    Reproduces a ``--server`` deployment whose host router is not mounted:
    ``GET /v1/hosts/{id}`` / ``GET /v1/runners/{id}/status`` fall through
    to the SPA HTML5-history fallback and answer ``200 text/html`` with
    ``index.html`` until (in this mock) the real status endpoint finally
    reports online. Before the non-JSON tolerance fix the first HTML body
    raised ``json.JSONDecodeError`` out of the wait, crashing the REPL
    before it became ready.

    :param html_polls: Number of initial 200-text/html responses to serve
        before answering with the JSON ``body``.
    :param body: JSON body returned once "online".
    """

    def __init__(self, html_polls: int, body: dict[str, object]) -> None:
        self.html_polls = html_polls
        self.body = body
        self.requests_seen = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """
        Handle one mock-transport request.

        :param request: The outgoing request the client built.
        :returns: A ``200 text/html`` SPA-fallback response until the
            HTML budget is exhausted, then a ``200`` JSON ``body``.
        """
        self.requests_seen += 1
        if self.requests_seen <= self.html_polls:
            return httpx.Response(
                200,
                text="<!doctype html><title>omnigent</title>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(200, json=self.body)


class _AlwaysHtml:
    """MockTransport handler that always answers the SPA HTML fallback (200 text/html)."""

    def __init__(self) -> None:
        self.requests_seen = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """
        Answer with the SPA HTML5-history fallback body.

        :param request: The outgoing request the client built.
        :returns: A ``200 text/html`` response carrying ``index.html``.
        """
        self.requests_seen += 1
        return httpx.Response(
            200,
            text="<!doctype html><title>omnigent</title>",
            headers={"content-type": "text/html"},
        )


@pytest.fixture
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the poll intervals so the wait loops iterate in milliseconds.

    Patches the module's own constants (read at call time by
    ``daemon_poll_intervals``), keeping each test well under 100ms
    instead of multiples of the real cadence. Both the opening interval
    and the steady-state cap are shrunk — leaving the opening one at its
    real value would dominate these short loops.
    """
    monkeypatch.setattr(daemon_launch, "DAEMON_POLL_INITIAL_INTERVAL_S", 0.01)
    monkeypatch.setattr(daemon_launch, "DAEMON_POLL_INTERVAL_S", 0.01)


async def test_wait_for_runner_online_polls_through_transient_connect_errors(
    fast_poll: None,
) -> None:
    """Two refused status polls must not fail the wait once the runner comes up.

    Reproduces the CI shape: the local server briefly refuses
    connections, then recovers within the deadline. Before the
    tolerance fix the first refused poll propagated
    ``httpx.ConnectError`` out of the wait, so this test failing with
    a raised ``ConnectError`` means the tolerance regressed.
    """
    handler = _FlakyThenOnline(failures=2, body={"online": True})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        await wait_for_runner_online(client, "runner_abc123", timeout_s=5.0)
    # 3 = 2 refused polls + the one that observed "online". Fewer means
    # the refusals never happened (test setup bug); a raise instead of
    # 3 means the tolerance regressed.
    assert handler.requests_seen == 3


async def test_wait_for_runner_online_fails_fast_on_exit_report(
    fast_poll: None,
) -> None:
    """A reported runner death ends the wait immediately with the cause.

    The host daemon watches its spawned runners; when one dies before
    connecting, the status endpoint answers ``online: false`` with an
    ``error`` carrying the daemon-composed cause (exit code + log
    tail). The wait must surface that error on the first poll that
    sees it — a dead process can never come online, so polling out the
    full deadline would only hide the cause behind a generic timeout.
    """
    daemon_error = (
        "runner process exited with code 1 (log on host: ~/x.log)\n"
        "--- runner log tail ---\nModuleNotFoundError: No module named 'claude_agent_sdk'"
    )
    handler = _FlakyThenOnline(
        failures=0,
        body={"runner_id": "runner_abc123", "online": False, "error": daemon_error},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        with pytest.raises(click.ClickException) as excinfo:
            # Generous deadline on purpose: a fail-fast regression makes
            # this poll ~500 iterations (5s / 0.01s) and raise the
            # generic timeout message instead, failing the asserts below.
            await wait_for_runner_online(client, "runner_abc123", timeout_s=5.0)
    message = str(excinfo.value)
    assert "runner_abc123" in message
    # The daemon's full cause — including the log tail line that names
    # the actual failure — must reach the user verbatim.
    assert daemon_error in message
    # Exactly one poll: the first response already carried the death
    # report. More polls mean the error field was ignored at least once.
    assert handler.requests_seen == 1


async def test_wait_for_runner_online_keeps_polling_without_exit_report(
    fast_poll: None,
) -> None:
    """``online: false`` with no error keeps polling to the deadline.

    A runner that is merely still starting reports offline with no
    ``error`` field — the wait must NOT fail fast on that (it would
    break every normal launch), and the deadline failure keeps the
    generic guidance message.
    """
    handler = _FlakyThenOnline(failures=0, body={"runner_id": "runner_abc123", "online": False})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        with pytest.raises(click.ClickException) as excinfo:
            await wait_for_runner_online(client, "runner_abc123", timeout_s=0.05)
    # Multiple polls prove the offline-without-error answer did not
    # trigger the fail-fast path; 1 would mean a plain "starting"
    # runner is being treated as dead.
    assert handler.requests_seen > 1
    assert "did not connect within" in str(excinfo.value)


async def test_wait_for_runner_online_deadline_reports_last_connect_error(
    fast_poll: None,
) -> None:
    """A never-online runner fails at the deadline, naming the last error.

    The wait must end in ``click.ClickException`` (the CLI's friendly
    failure), not a raw ``httpx.ConnectError``, and the message must
    carry the last transport error so the user sees WHY the runner
    never came online.
    """
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_AlwaysRefuses()), base_url="http://test"
    ) as client:
        with pytest.raises(click.ClickException) as excinfo:
            await wait_for_runner_online(client, "runner_abc123", timeout_s=0.05)
    message = str(excinfo.value)
    assert "runner_abc123" in message
    # The diagnostic hook: without the last-error detail the user gets
    # "did not connect" with no clue the server was refusing TCP.
    assert "Last connection error" in message
    assert "connection refused" in message


async def test_wait_for_host_online_polls_through_transient_connect_errors(
    fast_poll: None,
) -> None:
    """Two refused host polls must not fail the wait once the daemon registers.

    Same tolerance contract as the runner wait, for the host status
    endpoint's ``{"status": "online"}`` shape.
    """
    handler = _FlakyThenOnline(failures=2, body={"status": "online"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        await wait_for_host_online(client, "host_abc123", timeout_s=5.0)
    # 3 = 2 refused polls + the one that observed "online" (see the
    # runner-side twin test for the failure-direction reading).
    assert handler.requests_seen == 3


async def test_wait_for_host_online_deadline_reports_last_connect_error(
    fast_poll: None,
) -> None:
    """A never-reachable host fails at the deadline, naming the last error."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_AlwaysRefuses()), base_url="http://test"
    ) as client:
        with pytest.raises(click.ClickException) as excinfo:
            await wait_for_host_online(client, "host_abc123", timeout_s=0.05)
    message = str(excinfo.value)
    assert "host_abc123" in message
    assert "Last connection error" in message
    assert "connection refused" in message
    assert "Check that this machine can reach http://test" in message


@pytest.mark.parametrize(
    ("status_code", "headers", "body"),
    [
        pytest.param(401, {}, None, id="unauthorized"),
        pytest.param(403, {}, "Invalid Token", id="invalid-token"),
        pytest.param(
            302,
            {"location": "https://ws.cloud.databricks.com/oidc/v1/authorize?client_id=app"},
            None,
            id="sign-in-redirect",
        ),
    ],
)
async def test_wait_for_host_online_deadline_names_rejected_sign_in(
    fast_poll: None,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    headers: dict[str, str],
    body: str | None,
) -> None:
    """A host poll rejected for sign-in names the login remedy, not reachability.

    The Databricks Apps front door answers a missing or expired bearer with
    401/403 or a 302 to its OAuth login flow. The server was reachable, so
    the reachability and stale-host hints would misdirect the user.
    """
    monkeypatch.setenv("OMNIGENT_WRAPPER_COMMAND", "isaac omni")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_AlwaysAnswers(status_code, headers, body)),
        base_url="https://ws.cloud.databricks.com/api/2.0/omnigent",
    ) as client:
        with pytest.raises(click.ClickException) as excinfo:
            await wait_for_host_online(client, "host_abc123", timeout_s=0.05)
    message = str(excinfo.value)
    assert "host_abc123" in message
    assert f"The server rejected this machine's sign-in (HTTP {status_code})." in message
    assert "Run `isaac omni login https://ws.cloud.databricks.com/omnigent` and retry." in message
    assert "can reach" not in message
    assert "another account" not in message
    assert suppresses_recovery_hint(excinfo.value)


async def test_wait_for_host_online_deadline_names_host_owned_by_another_account(
    fast_poll: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host registered by a different account names the ownership remedies.

    Signing in again as the same account cannot help, and restarting the
    daemon cannot change who owns the host id.
    """
    monkeypatch.setenv("OMNIGENT_WRAPPER_COMMAND", "isaac omni")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_AlwaysAnswers(403, {}, {"detail": "not your host"})),
        base_url="https://ws.cloud.databricks.com/api/2.0/omnigent",
    ) as client:
        with pytest.raises(click.ClickException) as excinfo:
            await wait_for_host_online(client, "host_abc123", timeout_s=0.05)
    message = str(excinfo.value)
    assert "The server says another account owns this host (HTTP 403)." in message
    assert (
        "Run `isaac omni login https://ws.cloud.databricks.com/omnigent` as the account"
        " that registered it, or run `isaac omni host stop --all` and then"
        " `isaac omni host reset-id` to register this machine as a new host, then retry."
    ) in message
    assert "rejected this machine's sign-in" not in message
    assert suppresses_recovery_hint(excinfo.value)


async def test_wait_for_host_online_deadline_reports_reachability_when_answers_stop(
    fast_poll: None,
) -> None:
    """Refused connections after an early sign-in rejection report reachability, not login."""
    handler = _FirstThenRest(first_status=401, rest_status=None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        with pytest.raises(click.ClickException) as excinfo:
            await wait_for_host_online(client, "host_abc123", timeout_s=0.05)
    message = str(excinfo.value)
    assert handler.requests_seen > 1
    assert "Last connection error: ConnectError('connection refused')." in message
    assert "Check that this machine can reach http://test" in message
    assert "login" not in message
    assert not suppresses_recovery_hint(excinfo.value)


async def test_wait_for_host_online_deadline_drops_connect_error_once_server_answers(
    fast_poll: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sign-in rejection after an early refused connection names only the login remedy."""
    monkeypatch.delenv("OMNIGENT_WRAPPER_COMMAND", raising=False)
    handler = _FirstThenRest(first_status=None, rest_status=401)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        with pytest.raises(click.ClickException) as excinfo:
            await wait_for_host_online(client, "host_abc123", timeout_s=0.05)
    message = str(excinfo.value)
    assert handler.requests_seen > 1
    assert "Last connection error" not in message
    assert "The server rejected this machine's sign-in (HTTP 401)." in message
    assert "Run `omnigent login http://test` and retry." in message
    assert suppresses_recovery_hint(excinfo.value)


@pytest.mark.parametrize(
    ("status_code", "headers", "body"),
    [
        pytest.param(200, {}, {"status": "offline"}, id="registered-offline"),
        pytest.param(404, {}, {"detail": "host not found"}, id="not-registered"),
        pytest.param(307, {"location": "/v1/hosts/host_abc123/"}, None, id="unrelated-redirect"),
    ],
)
async def test_wait_for_host_online_deadline_points_at_host_logs_when_server_answers(
    fast_poll: None,
    status_code: int,
    headers: dict[str, str],
    body: dict[str, object] | None,
) -> None:
    """A server that answers while the daemon stays offline points at the daemon's logs.

    The daemon writes its own tunnel failures (including its auth rejections)
    to the host log, so that is where the cause is, not this machine's reachability.
    """
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_AlwaysAnswers(status_code, headers, body)),
        base_url="http://test",
    ) as client:
        with pytest.raises(click.ClickException) as excinfo:
            await wait_for_host_online(client, "host_abc123", timeout_s=0.05)
    message = str(excinfo.value)
    assert "host_abc123" in message
    assert f"The server answered (HTTP {status_code})" in message
    assert f"logs under {process_log_dir_reference('host')}" in message
    assert "can reach" not in message
    assert "login" not in message
    assert not suppresses_recovery_hint(excinfo.value)


async def test_wait_for_runner_online_tolerates_html_fallback_then_online(
    fast_poll: None,
) -> None:
    """A 200-text/html status body must not crash the runner wait.

    Reproduces the ``--server`` deployment where the status path falls
    through to the SPA HTML5-history fallback for the first polls, then
    the runner registers and the endpoint answers JSON ``online: true``.
    Before the non-JSON tolerance fix the first HTML body raised
    ``json.JSONDecodeError`` out of the wait; this test failing with a
    raised ``ValueError`` means that regressed.
    """
    handler = _HtmlThenOnline(html_polls=2, body={"online": True})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        await wait_for_runner_online(client, "runner_abc123", timeout_s=5.0)
    # 3 = 2 HTML-fallback polls + the one that observed JSON "online".
    assert handler.requests_seen == 3


async def test_wait_for_runner_online_html_fallback_fails_with_timeout_not_jsonerror(
    fast_poll: None,
) -> None:
    """A status path stuck on the HTML fallback fails with the friendly timeout.

    When the host router is never mounted the status path answers
    ``200 text/html`` forever. The wait must keep polling and end in
    ``click.ClickException`` (the actionable timeout) rather than
    propagating ``json.JSONDecodeError`` on the first poll.
    """
    handler = _AlwaysHtml()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        with pytest.raises(click.ClickException) as excinfo:
            await wait_for_runner_online(client, "runner_abc123", timeout_s=0.05)
    assert "runner_abc123" in str(excinfo.value)
    # >1 proves the HTML body was tolerated as "no status yet" and polling
    # continued; a raised JSONDecodeError would never reach the asserts.
    assert handler.requests_seen > 1


async def test_runner_is_online_false_on_html_fallback_body() -> None:
    """``runner_is_online`` reports False (not a crash) on a 200 HTML body.

    The single-shot reuse check must treat a non-JSON 200 as "not online"
    so ``launch_or_reuse_daemon_runner`` falls back to launching a fresh
    runner instead of dying on ``resp.json()``.
    """
    handler = _AlwaysHtml()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        assert await runner_is_online(client, "runner_abc123") is False


async def test_wait_for_host_online_tolerates_html_fallback_then_online(
    fast_poll: None,
) -> None:
    """Same non-JSON tolerance for the host status endpoint's online wait."""
    handler = _HtmlThenOnline(html_polls=2, body={"status": "online"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        await wait_for_host_online(client, "host_abc123", timeout_s=5.0)
    # 3 = 2 HTML-fallback polls + the one that observed JSON "online".
    assert handler.requests_seen == 3


async def test_open_daemon_client_pins_slice_key_on_workspace_mount() -> None:
    """On the workspace mount the client is pinned to the host's replica.

    The host_id routing header is baked into the client's default headers, so
    every request it makes — the launch, runner-status polls, and the
    session/terminal ops (including a resume reattach check that runs before
    the launch) — reaches the replica holding the host's tunnels. Base headers
    are preserved.
    """
    async with open_daemon_client(
        "https://ws.example.com/api/2.0/omnigent",
        {"Authorization": "Bearer t"},
        "host_abc123",
    ) as client:
        assert client.headers.get(OMNIGENT_SLICE_KEY_HEADER) == "host_abc123"
        assert client.headers.get("Authorization") == "Bearer t"


async def test_open_daemon_client_no_slice_key_off_workspace() -> None:
    """An unsharded server has no sharding layer, so no key is baked."""
    async with open_daemon_client("http://test", {}, "host_abc123") as client:
        assert OMNIGENT_SLICE_KEY_HEADER not in client.headers


async def test_open_daemon_client_no_slice_key_without_host() -> None:
    """A hostless (local) session leaves routing to the default fallback."""
    async with open_daemon_client("https://ws.example.com/api/2.0/omnigent", {}, None) as client:
        assert OMNIGENT_SLICE_KEY_HEADER not in client.headers


def test_daemon_poll_intervals_open_tight_then_hold_at_the_cadence() -> None:
    """
    Readiness probes start tight and ease off to the steady cadence.

    These waits gate every native-harness launch and usually resolve in
    the first probe or two, so the opening interval must be well under
    the steady cadence — otherwise a resource that became ready
    immediately still costs a full interval of dead time. The sequence
    must also be monotonic and never exceed the cadence, so a long wait
    does not hammer the server.
    """
    intervals = daemon_launch.daemon_poll_intervals()
    first_ten = [next(intervals) for _ in range(10)]

    assert first_ten[0] == daemon_launch.DAEMON_POLL_INITIAL_INTERVAL_S
    assert first_ten[0] < daemon_launch.DAEMON_POLL_INTERVAL_S
    assert first_ten == sorted(first_ten)
    assert max(first_ten) == daemon_launch.DAEMON_POLL_INTERVAL_S
    # Reaching "ready" on the third probe must cost less than the old flat
    # cadence would have spent getting there.
    assert sum(first_ten[:2]) < 2 * daemon_launch.DAEMON_POLL_INTERVAL_S
