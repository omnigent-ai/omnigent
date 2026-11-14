"""Session-scoped runner requests are keyed by the target session's host."""

import asyncio
import io
import time
from uuid import uuid4

import httpx
import pytest

from omnigent import session_host
from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, UNKEYED_REQUEST_EXTENSION
from omnigent.runner.session_requests import post_on_host, remember_session_host
from omnigent.session_host import (
    REQUEST_HOST_EXTENSION,
    SessionRoutingTransport,
    get_session_host,
    set_session_host,
)

RUNNER_HOST = "runner-host"
TARGET_HOST = "target-host"
MOVED_HOST = "moved-host"
PARENT = "1234567890123457"
CHILD = "1234567890123458"
EVENTS = f"/v1/sessions/{CHILD}/events"
PAYLOAD = {"type": "session_message", "data": {"message": "Work"}}


@pytest.fixture(autouse=True)
def isolated_host_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setattr(session_host, "_session_hosts", {})


def make_client(respond) -> httpx.AsyncClient:
    """A plain client over the routing transport, keyed by the runner's own host."""
    return httpx.AsyncClient(
        transport=SessionRoutingTransport(transport=httpx.MockTransport(respond)),
        base_url="http://server",
        headers={OMNIGENT_SLICE_KEY_HEADER: RUNNER_HOST},
    )


UNNAMED = object()


class Replicas:
    """Sessions live on one host: that key is served, other keys get wrong_replica.

    ``snapshots`` maps a session id to its snapshot body, for lookups.
    """

    def __init__(
        self,
        owner: str | None,
        snapshots: dict[str, dict] | None = None,
        *,
        named: object = UNNAMED,
    ) -> None:
        self.owner = owner
        self.snapshots = snapshots or {}
        self.named = named
        self.sends: list[str | None] = []
        self.lookups: list[httpx.Request] = []
        self.wrong_replicas = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        # Like ingress: the header wins, then the query parameter.
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER) or request.url.params.get(
            "omnigent_slice_key"
        )
        if is_lookup(request):
            self.lookups.append(request)
            session_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=self.snapshots.get(session_id, {"runner_id": "r"}))
        self.sends.append(key)
        if key == self.owner:
            return httpx.Response(202, json={"queued": True})
        self.wrong_replicas += 1
        return wrong_replica() if self.named is UNNAMED else named_refusal(self.named)


def is_lookup(request: httpx.Request) -> bool:
    """The host lookup is the bare session snapshot read, with its four exclusion params."""
    return (
        request.method == "GET"
        and request.url.path.count("/") == 3
        and request.url.path.startswith("/v1/sessions/")
        and "include_liveness" in request.url.params
    )


def snapshot(host_id: str | None = None, **extra) -> dict:
    body = {"runner_id": "runner", **extra}
    if host_id is not None:
        body["host_id"] = host_id
    return body


def wrong_replica() -> httpx.Response:
    return httpx.Response(400, json={"error": {"code": "wrong_replica"}})


def named_refusal(slice_key: str | None) -> httpx.Response:
    """A keyed host's refusal: the body names the key to resend with, or null for keyless."""
    error = {"code": "wrong_replica", "slice_key": slice_key}
    return httpx.Response(400, json={"error": error})


@pytest.mark.asyncio
async def test_cache_hit_keys_the_request_by_its_host_without_a_lookup():
    set_session_host(CHILD, TARGET_HOST)
    replicas = Replicas(TARGET_HOST)
    async with make_client(replicas) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 202
    assert replicas.sends == [TARGET_HOST]
    assert replicas.lookups == []


@pytest.mark.asyncio
async def test_cache_miss_sends_the_callers_key_without_a_lookup():
    replicas = Replicas(RUNNER_HOST)
    async with make_client(replicas) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 202
    assert replicas.sends == [RUNNER_HOST]
    assert replicas.lookups == []


@pytest.mark.asyncio
async def test_wrong_replica_looks_up_the_host_and_retries_once_on_it():
    replicas = Replicas(TARGET_HOST, {CHILD: snapshot(TARGET_HOST)})
    async with make_client(replicas) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 202
    assert replicas.sends == [RUNNER_HOST, TARGET_HOST]
    assert len(replicas.lookups) == 1
    assert replicas.wrong_replicas == 1
    assert get_session_host(CHILD) == TARGET_HOST


@pytest.mark.asyncio
async def test_refusal_naming_the_host_retries_on_it_without_a_lookup():
    replicas = Replicas(TARGET_HOST, named=TARGET_HOST)
    async with make_client(replicas) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 202
    assert replicas.sends == [RUNNER_HOST, TARGET_HOST]
    assert replicas.lookups == []
    assert get_session_host(CHILD) == TARGET_HOST


@pytest.mark.asyncio
async def test_refusal_naming_no_key_resends_keyless_without_a_lookup():
    replicas = Replicas(None, named=None)
    params = {"omnigent_slice_key": TARGET_HOST, "keep": "1"}
    async with make_client(replicas) as client:
        response = await client.post(EVENTS, params=params, json=PAYLOAD)
    assert response.status_code == 202
    assert replicas.sends == [RUNNER_HOST, None]
    assert replicas.lookups == []
    assert dict(response.request.url.params) == {"keep": "1"}


@pytest.mark.asyncio
async def test_named_retry_refused_again_goes_keyless_once_without_a_lookup():
    replicas = Replicas("nobody", named=TARGET_HOST)
    async with make_client(replicas) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 400
    assert replicas.sends == [RUNNER_HOST, TARGET_HOST, None]
    assert replicas.lookups == []


@pytest.mark.asyncio
async def test_session_on_another_host_costs_one_wrong_replica_then_routes_correctly():
    """A session the runner did not create costs one refusal, then routes correctly."""
    replicas = Replicas(TARGET_HOST, {CHILD: snapshot(TARGET_HOST)})
    async with make_client(replicas) as client:
        first = await client.post(EVENTS, json=PAYLOAD)
        second = await client.post(EVENTS, json=PAYLOAD)
        items = await client.get(f"/v1/sessions/{CHILD}/items")
    assert first.status_code == second.status_code == items.status_code == 202
    assert replicas.wrong_replicas == 1
    assert replicas.sends == [RUNNER_HOST, TARGET_HOST, TARGET_HOST, TARGET_HOST]
    assert len(replicas.lookups) == 1


@pytest.mark.asyncio
async def test_moved_host_is_re_resolved_and_retried_once_on_the_new_owner():
    set_session_host(CHILD, TARGET_HOST)
    replicas = Replicas(MOVED_HOST, {CHILD: snapshot(MOVED_HOST)})
    async with make_client(replicas) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 202
    assert replicas.sends == [TARGET_HOST, MOVED_HOST]
    assert get_session_host(CHILD) == MOVED_HOST


@pytest.mark.asyncio
async def test_unchanged_host_retries_keyless_once_for_hosts_that_predate_keying():
    set_session_host(CHILD, TARGET_HOST)
    sends: list[str | None] = []

    def legacy(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=snapshot(TARGET_HOST))
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        sends.append(key)
        return httpx.Response(202, json={"queued": True}) if key is None else wrong_replica()

    async with make_client(legacy) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 202
    assert sends == [TARGET_HOST, None]


@pytest.mark.asyncio
async def test_failed_lookup_sends_keyless_once_and_keeps_the_cache():
    set_session_host(CHILD, TARGET_HOST)
    sends: list[str | None] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(500)
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        sends.append(key)
        return httpx.Response(202, json={"queued": True}) if key is None else wrong_replica()

    async with make_client(flaky) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 202
    assert sends == [TARGET_HOST, None]
    assert get_session_host(CHILD) == TARGET_HOST


@pytest.mark.asyncio
async def test_timed_out_lookup_sends_keyless_once_with_a_bounded_wait(monkeypatch):
    monkeypatch.setattr(session_host, "SESSION_HOST_LOOKUP_TIMEOUT_S", 0.08)
    set_session_host(CHILD, TARGET_HOST)
    lookups = []
    sends = []
    cancelled = False

    async def slow_lookup(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled
        if request.method == "GET":
            lookups.append(request.url.path)
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                cancelled = True
                raise
            return httpx.Response(200, json=snapshot(parent_session_id=PARENT))
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        sends.append(key)
        return httpx.Response(202, json={"queued": True}) if key is None else wrong_replica()

    start = time.monotonic()
    async with make_client(slow_lookup) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 202
    assert sends == [TARGET_HOST, None]
    assert len(lookups) == 2
    assert cancelled
    assert time.monotonic() - start < 0.5


@pytest.mark.asyncio
async def test_hostless_session_after_wrong_replica_is_tried_keyless_once():
    replicas = Replicas(None, {CHILD: snapshot()})
    async with make_client(replicas) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 202
    assert replicas.sends == [RUNNER_HOST, None]
    assert len(replicas.lookups) == 1


@pytest.mark.asyncio
async def test_child_session_resolves_through_parent_host():
    replicas = Replicas(
        TARGET_HOST,
        {CHILD: snapshot(parent_session_id=PARENT), PARENT: snapshot(TARGET_HOST)},
    )
    async with make_client(replicas) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 202
    assert replicas.sends == [RUNNER_HOST, TARGET_HOST]
    assert [request.url.path for request in replicas.lookups] == [
        f"/v1/sessions/{CHILD}",
        f"/v1/sessions/{PARENT}",
    ]
    assert get_session_host(CHILD) == TARGET_HOST


@pytest.mark.asyncio
async def test_retry_never_loops_when_every_answer_is_wrong_replica():
    set_session_host(CHILD, TARGET_HOST)
    # No key is served, so the keyless attempt is refused too.
    replicas = Replicas("nobody", {CHILD: snapshot(MOVED_HOST)})
    async with make_client(replicas) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == 400
    assert replicas.sends == [TARGET_HOST, MOVED_HOST, None]
    assert len(replicas.lookups) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 503])
async def test_other_failures_are_returned_not_retried(status: int):
    set_session_host(CHILD, TARGET_HOST)
    sends = []

    def failing(request: httpx.Request) -> httpx.Response:
        sends.append(request.headers.get(OMNIGENT_SLICE_KEY_HEADER))
        return httpx.Response(status, json={"error": {"code": "boom"}})

    async with make_client(failing) as client:
        response = await client.post(EVENTS, json=PAYLOAD)
    assert response.status_code == status
    assert sends == [TARGET_HOST]


@pytest.mark.asyncio
async def test_timeouts_are_never_retried():
    set_session_host(CHILD, TARGET_HOST)
    sends = []

    def uncertain(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            raise AssertionError("a timed-out delivery must not trigger a lookup")
        sends.append(request.headers.get(OMNIGENT_SLICE_KEY_HEADER))
        raise httpx.ReadTimeout("delivery uncertain")

    async with make_client(uncertain) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post(EVENTS, json=PAYLOAD)
    assert sends == [TARGET_HOST]


@pytest.mark.asyncio
async def test_established_streams_are_keyed_but_never_inspected():
    set_session_host(CHILD, TARGET_HOST)
    sends = []

    def stream(request: httpx.Request) -> httpx.Response:
        if is_lookup(request):
            raise AssertionError("a stream must not trigger a lookup")
        sends.append(request.headers.get(OMNIGENT_SLICE_KEY_HEADER))
        return httpx.Response(
            400,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"error": {"code": "wrong_replica"}}\n\n',
        )

    async with make_client(stream) as client:
        async with client.stream("GET", f"/v1/sessions/{CHILD}/stream") as response:
            assert response.status_code == 400
    assert sends == [TARGET_HOST]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/sessions/orchestrated"),
        ("POST", "/v1/sessions/orchestrated/projects"),
        ("GET", "/v1/sessions/projects"),
    ],
)
async def test_collection_routes_are_untouched(method: str, path: str):
    sends = []

    def refuse(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith(f"/v1/sessions/{CHILD}"):
            raise AssertionError("collection routes are not session lookups")
        sends.append(request.headers.get(OMNIGENT_SLICE_KEY_HEADER))
        return wrong_replica()

    async with make_client(refuse) as client:
        response = await client.request(method, path, json={})
    assert response.status_code == 400
    assert sends == [RUNNER_HOST]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/sessions", "/v1/sessions/orchestrated"])
async def test_create_body_host_id_keys_the_create(path: str):
    replicas = Replicas(TARGET_HOST)
    async with make_client(replicas) as client:
        response = await client.post(path, json={"agent_id": "a", "host_id": TARGET_HOST})
    assert response.status_code == 202
    assert replicas.sends == [TARGET_HOST]
    assert replicas.lookups == []


@pytest.mark.asyncio
async def test_create_without_host_id_keeps_the_callers_key():
    replicas = Replicas(RUNNER_HOST)
    async with make_client(replicas) as client:
        response = await client.post("/v1/sessions", json={"agent_id": "a"})
    assert response.status_code == 202
    assert replicas.sends == [RUNNER_HOST]


@pytest.mark.asyncio
async def test_unkeyed_request_is_sent_without_a_key_or_lookup():
    replicas = Replicas(None)
    async with make_client(replicas) as client:
        request = client.build_request("POST", EVENTS, json=PAYLOAD)
        request.extensions[UNKEYED_REQUEST_EXTENSION] = True
        response = await client.send(request)
    assert response.status_code == 202
    assert replicas.sends == [None]
    assert replicas.lookups == []


@pytest.mark.asyncio
async def test_non_session_routes_keep_the_runner_key_without_lookup():
    replicas = Replicas(RUNNER_HOST)
    async with make_client(replicas) as client:
        response = await client.get("/v1/info")
    assert response.status_code == 202
    assert replicas.sends == [RUNNER_HOST]
    assert replicas.lookups == []


@pytest.mark.asyncio
async def test_post_on_host_sends_an_empty_host_keyless():
    replicas = Replicas(None)
    async with make_client(replicas) as client:
        response = await post_on_host(client, "/v1/sessions", host_id=None, json={"agent_id": "a"})
    assert response.status_code == 202
    assert replicas.sends == [None]


@pytest.mark.asyncio
async def test_post_on_host_marks_the_request_with_its_host():
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions[REQUEST_HOST_EXTENSION])
        return httpx.Response(202, json={"queued": True})

    async with make_client(record) as client:
        await post_on_host(client, "/v1/sessions", host_id=TARGET_HOST, json={"agent_id": "a"})
    assert seen == [TARGET_HOST]


def test_create_response_fills_the_cache_for_a_later_request():
    session_id = str(uuid4())
    remember_session_host(session_id, TARGET_HOST)
    assert get_session_host(session_id.replace("-", "")) == TARGET_HOST


@pytest.mark.parametrize("session_id", [None, "", "not-a-session", "orchestrated"])
def test_create_response_without_a_usable_identity_caches_nothing(session_id):
    remember_session_host(session_id, TARGET_HOST)
    assert session_host._session_hosts == {}


def test_create_response_without_a_host_leaves_the_cache_alone():
    set_session_host(CHILD, TARGET_HOST)
    remember_session_host(CHILD, None)
    assert get_session_host(CHILD) == TARGET_HOST


@pytest.mark.asyncio
async def test_runner_own_session_is_prefilled_so_its_first_request_needs_no_lookup():
    remember_session_host(PARENT, TARGET_HOST)
    replicas = Replicas(TARGET_HOST)
    async with make_client(replicas) as client:
        response = await client.post(f"/v1/sessions/{PARENT}/events", json=PAYLOAD)
    assert response.status_code == 202
    assert replicas.sends == [TARGET_HOST]
    assert replicas.lookups == []


@pytest.mark.asyncio
async def test_json_refusal_before_stream_establishment_can_recover():
    set_session_host(CHILD, TARGET_HOST)
    sends = []

    def stream(request: httpx.Request) -> httpx.Response:
        key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
        sends.append(key)
        if len(sends) == 1:
            return httpx.Response(
                400, json={"error": {"code": "wrong_replica", "slice_key": MOVED_HOST}}
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"data: ok\n\n"
        )

    async with make_client(stream) as client:
        async with client.stream("GET", f"/v1/sessions/{CHILD}/stream") as response:
            assert await response.aread() == b"data: ok\n\n"
    assert sends == [TARGET_HOST, MOVED_HOST]


class UploadSource(io.BytesIO):
    """An upload's file, counting how often its content is read from the start."""

    def __init__(self, data: bytes, *, seekable: bool) -> None:
        super().__init__(data)
        self._seekable = seekable
        self.passes = 0

    def seekable(self) -> bool:
        return self._seekable

    def seek(self, offset: int, whence: int = 0) -> int:
        if not self._seekable:
            raise io.UnsupportedOperation("seek")
        return super().seek(offset, whence)

    def read(self, size: int | None = -1) -> bytes:
        if self.tell() == 0 and size != 0:
            self.passes += 1
        return super().read(size)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seekable", "passes"), [(True, 2), (False, 1)], ids=["file-streams", "pipe-buffered"]
)
async def test_refused_upload_reaches_the_host_with_the_whole_file(
    seekable: bool, passes: int
) -> None:
    """A file is re-read for the retry instead of held in memory; a pipe is buffered once."""
    data = bytes(range(256)) * 1024
    source = UploadSource(data, seekable=seekable)
    bodies: list[tuple[str | None, bool]] = []

    class Network(httpx.AsyncBaseTransport):
        """Reads each body as a socket send does; MockTransport would buffer it first."""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            key = request.headers.get(OMNIGENT_SLICE_KEY_HEADER)
            assert isinstance(request.stream, httpx.AsyncByteStream)
            body = b"".join([chunk async for chunk in request.stream])
            bodies.append((key, data in body))
            if key != TARGET_HOST:
                return named_refusal(TARGET_HOST)
            return httpx.Response(201, json={"id": "file_1"})

    async with httpx.AsyncClient(
        transport=SessionRoutingTransport(transport=Network()),
        base_url="http://server",
        headers={OMNIGENT_SLICE_KEY_HEADER: RUNNER_HOST},
    ) as client:
        response = await client.post(
            f"/v1/sessions/{CHILD}/resources/files", files={"file": ("a.bin", source)}
        )
    assert response.status_code == 201
    assert bodies == [(RUNNER_HOST, True), (TARGET_HOST, True)]
    assert source.passes == passes
