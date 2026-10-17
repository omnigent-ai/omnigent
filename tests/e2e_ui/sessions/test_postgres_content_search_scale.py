"""E2E: an absent-term session search on a ~985k-item PostgreSQL deployment answers in
time once the trigram index is built and ``OMNIGENT_PG_CONTENT_SEARCH=auto`` is set.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.compat import apply_server_env, server_executable
from tests.e2e_ui.conftest import _TEST_AGENT_YAML, _build_hello_world_bundle

_REPO_ROOT = Path(__file__).resolve().parents[3]

_CONVERSATIONS = 100
# Reported deployment: ~985k items. Override to trade fidelity for speed.
_ITEMS = int(os.environ.get("OMNIGENT_E2E_PG_SEARCH_ITEMS", "985000"))
# ~3.2 KB of prose-like search_text per item, ~3 GB in total. A small English
# vocabulary keeps the trigram index (and its build's sort spill) CI-sized;
# random hex needs several times the table in temporary space.
_WORDS_PER_ITEM = 460
_VOCABULARY = [
    "about", "above", "action", "after", "again", "allow", "among", "around",
    "because", "before", "better", "between", "budget", "build", "change", "chapter",
    "commit", "common", "create", "deploy", "detail", "during", "early", "effect",
    "enough", "every", "follow", "forward", "future", "gather", "ground", "handle",
    "history", "import", "inside", "itself", "latest", "little", "manage", "member",
    "method", "minute", "modern", "moment", "nothing", "number", "object", "often",
    "order", "other", "people", "period", "person", "place", "point", "policy",
    "power", "present", "problem", "process", "program", "project", "public", "record",
]  # fmt: skip
# Letters absent from the vocabulary keep this term absent from every item.
_ABSENT_TERM = "zzqx-absent-term"
# useConversations.ts SEARCH_FETCH_TIMEOUT_MS.
_CLIENT_DEADLINE_MS = 10_000
_HEALTH_TIMEOUT_S = 120.0

_SEED_SQL = """
INSERT INTO conversation_items
  (workspace_id, conversation_id, id, response_id, created_at, status, position,
   type, data, search_text, created_by)
SELECT 0,
       decode(%(conv)s, 'hex'),
       decode(md5(random()::text || g::text), 'hex'),
       'resp_seed_' || (g / 2),
       %(now)s + g,
       1,
       g,
       1,
       '{"role":"assistant","content":[{"type":"output_text","text":"session note ' || g || '"}]}',
       'session note ' || g || ' ' || (
           SELECT string_agg(v[1 + ((g * 7919 + k * 104729) %% %(vocab_size)s)], ' ')
           FROM generate_series(1, %(words)s) k, (SELECT %(vocab)s::text[] AS v) vv
       ),
       NULL
FROM generate_series(0, %(count)s - 1) g"""


@dataclass(frozen=True)
class _SearchServer:
    base_url: str
    session_ids: list[str]
    # Outcome of ``omnigent debug db-build-search-index`` during setup.
    index_built: bool
    index_build_output: str


def _pg_bin_dir() -> Path | None:
    """Locate ``initdb``/``pg_ctl``: a packaged Postgres first, then ``PATH``."""
    candidates = sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True)
    if (found := shutil.which("pg_ctl")) is not None:
        candidates.append(Path(found).parent)
    for directory in candidates:
        if (directory / "initdb").exists() and (directory / "pg_ctl").exists():
            return directory
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def postgres_uri(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Init and start a throwaway PostgreSQL cluster; yield its SQLAlchemy URI."""
    bin_dir = _pg_bin_dir()
    if bin_dir is None:
        pytest.skip("PostgreSQL binaries (initdb/pg_ctl) are not installed")
    pytest.importorskip("psycopg")
    root = tmp_path_factory.mktemp("pg_search_cluster")
    data_dir = root / "data"
    port = _free_port()
    subprocess.run(
        [
            str(bin_dir / "initdb"),
            "-D",
            str(data_dir),
            "-U",
            "omnigent",
            "--auth=trust",
            "-E",
            "UTF8",
            "--locale=C.UTF-8",
        ],
        check=True,
        capture_output=True,
    )
    # The pytest tmp path is too long for a Unix socket; keep it under /tmp.
    socket_dir = tempfile.mkdtemp(prefix="pgsearch-", dir="/tmp")
    server_opts = (
        f"-p {port} -k {socket_dir} -c listen_addresses=127.0.0.1 -c fsync=off "
        "-c synchronous_commit=off -c full_page_writes=off -c shared_buffers=512MB "
        "-c maintenance_work_mem=512MB -c max_wal_size=2GB -c checkpoint_timeout=30min"
    )
    pg_ctl = [str(bin_dir / "pg_ctl"), "-D", str(data_dir), "-w", "-t", "120"]
    subprocess.run(
        [*pg_ctl, "-l", str(root / "postgres.log"), "-o", server_opts, "start"],
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(
            [
                str(bin_dir / "createdb"),
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-U",
                "omnigent",
                "omnigent",
            ],
            check=True,
            capture_output=True,
        )
        yield f"postgresql+psycopg://omnigent@127.0.0.1:{port}/omnigent"
    finally:
        subprocess.run([*pg_ctl, "-m", "fast", "stop"], check=False, capture_output=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


def _wait_healthy(proc: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early:\n{log_path.read_text()[-3000:]}")
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(
        f"server not healthy in {_HEALTH_TIMEOUT_S}s:\n{log_path.read_text()[-3000:]}"
    )


def _create_session(base_url: str, index: int) -> str:
    resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=60.0,
    )
    resp.raise_for_status()
    session_id = str(resp.json()["session_id"])
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": f"seeded session {index}"},
        timeout=30.0,
    ).raise_for_status()
    return session_id


def _seed_items(postgres_uri: str, session_ids: list[str], per_conversation: int) -> str:
    """Bulk-load message items in the store's row shape; return a size summary."""
    import psycopg

    dsn = postgres_uri.replace("postgresql+psycopg://", "postgresql://", 1)
    now = int(time.time())
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Seeding, not durability, is what this cluster is for.
        conn.execute("ALTER TABLE conversation_items SET UNLOGGED")
        for session_id in session_ids:
            conn.execute(
                _SEED_SQL,
                {
                    "conv": session_id,
                    "now": now,
                    "words": _WORDS_PER_ITEM,
                    "vocab": _VOCABULARY,
                    "vocab_size": len(_VOCABULARY),
                    "count": per_conversation,
                },
            )
        conn.execute("ANALYZE conversation_items")
        row = conn.execute(
            "SELECT count(*), pg_size_pretty(pg_total_relation_size('conversation_items')),"
            " pg_size_pretty(sum(octet_length(search_text))) FROM conversation_items"
        ).fetchone()
    assert row is not None
    return f"{row[0]} items, relation {row[1]}, search_text {row[2]}"


def _build_search_index(postgres_uri: str, env: dict[str, str]) -> tuple[bool, str]:
    """Run the operator command that builds the trigram index; return (ok, output)."""
    started = time.monotonic()
    result = subprocess.run(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "debug",
            "db-build-search-index",
            postgres_uri,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (
        f"exit {result.returncode} after {time.monotonic() - started:.0f}s\n"
        f"{result.stdout}{result.stderr}"
    )
    return result.returncode == 0, output


@pytest.fixture(scope="module")
def postgres_search_server(
    built_spa: None,
    postgres_uri: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_SearchServer]:
    """Spawn ``omnigent server`` on the seeded cluster in ``auto`` mode, then build the
    index through the CLI as an operator would; its outcome is asserted in the test body.
    """
    workdir = tmp_path_factory.mktemp("pg_search_server")
    agent_yaml = workdir / "hello_world.yaml"
    agent_yaml.write_text(_TEST_AGENT_YAML)
    artifacts = workdir / "artifacts"
    artifacts.mkdir()
    log_path = workdir / "server.log"
    port = _free_port()
    env: dict[str, str] = {
        **os.environ,
        # No turns run here; keep the harness off any real provider anyway.
        "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        "OMNIGENT_PG_CONTENT_SEARCH": "auto",
    }
    apply_server_env(env, _REPO_ROOT)
    log_handle = open(log_path, "wb")  # noqa: SIM115 — lives for the Popen lifetime
    proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            postgres_uri,
            "--artifact-location",
            str(artifacts),
            "--agent",
            str(agent_yaml),
        ],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_healthy(proc, base_url, log_path)
        session_ids = [_create_session(base_url, i) for i in range(_CONVERSATIONS)]
        print("seeded:", _seed_items(postgres_uri, session_ids, _ITEMS // _CONVERSATIONS))
        index_built, index_output = _build_search_index(postgres_uri, env)
        print("index build:", index_output.strip())
        yield _SearchServer(base_url, session_ids, index_built, index_output)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()
        cancelled = [
            line
            for line in log_path.read_text(errors="replace").splitlines()
            if "QueryCanceled" in line or "statement timeout" in line
        ]
        print(f"server log: {len(cancelled)} statement-timeout lines", *cancelled[:2], sep="\n  ")


def _timed_search(base_url: str, term: str) -> tuple[int, float, str]:
    """Issue the palette's search request over HTTP; return (status, seconds, body)."""
    started = time.monotonic()
    resp = httpx.get(
        f"{base_url}/v1/sessions",
        params={
            "order": "desc",
            "sort_by": "updated_at",
            "limit": 20,
            "visibility": "all",
            "search_query": term,
        },
        timeout=60.0,
    )
    return resp.status_code, time.monotonic() - started, resp.text[:300]


@pytest.mark.nightly
@pytest.mark.timeout(3600)
def test_absent_term_search_returns_within_client_deadline(
    page: Page,
    postgres_search_server: _SearchServer,
) -> None:
    """An absent term must settle to "No results found" before the client gives up."""
    server = postgres_search_server
    status, seconds, body = _timed_search(server.base_url, _ABSENT_TERM)
    print(f"HTTP search for {_ABSENT_TERM!r}: {status} in {seconds:.1f}s: {body}")

    page.goto(f"{server.base_url}/c/{server.session_ids[0]}")
    search_button = page.get_by_test_id("sidebar-search-button")
    expect(search_button).to_be_visible(timeout=30_000)
    search_button.click()

    dialog = page.get_by_role("dialog")
    palette_input = page.get_by_test_id("command-palette-input")
    expect(palette_input).to_be_visible()
    palette_input.fill(_ABSENT_TERM)

    no_results = dialog.get_by_text("No results found")
    settled = no_results.or_(dialog.get_by_role("status"))
    expect(settled.first).to_be_visible(timeout=_CLIENT_DEADLINE_MS + 5_000)
    expect(no_results).to_be_visible()
    expect(dialog.get_by_role("status")).to_have_count(0)

    assert status == 200 and seconds < _CLIENT_DEADLINE_MS / 1000, (
        f"search for an absent term answered HTTP {status} after {seconds:.1f}s "
        f"(client deadline {_CLIENT_DEADLINE_MS / 1000:.0f}s): {body}\n"
        f"index build: {server.index_build_output}"
    )
    # The fast answer must come from the operator-built index, not from luck.
    assert server.index_built, server.index_build_output
