"""E2E: workspace file paths in chat responses render as clickable links.

Regression guard for the home-relative (``~/...``) and absolute path
resolution layered on top of the chat path-linkification feature. A deterministic assistant message
(seeded via the ``external_assistant_message`` event — no LLM run)
mentions three paths in backticks:

  - ``~/<rel>/README.md`` — home-relative, resolves to the workspace root
    file ``README.md`` → link. This is the fix and the exact case the bug
    report hit; it exercises BOTH halves end-to-end: the runner emitting
    ``metadata.home`` and the frontend expanding ``~`` and stripping the root.
  - ``<abs-root>/README.md`` — absolute and under the root, resolves to
    ``README.md`` → link (the absolute-path half of the same fix; was
    rejected outright before).
  - ``/etc/hosts`` — absolute and OUTSIDE the workspace → stays inert code
    (must never linkify).

``~`` expansion can only land inside the workspace when the root is itself
under the runner's home. The default e2e workspace lives under ``$TMPDIR``
(not home), so this test pins the agent's ``os_env.cwd`` to a fresh directory
under ``Path.home()``: with no ``OMNIGENT_RUNNER_WORKSPACE`` set (the e2e
runner inherits none), ``compute_default_env_root`` uses that absolute cwd, so
the root is deterministically under home in both CI and local runs. The tilde
and absolute paths are still derived from the *live* ``metadata.root`` /
``metadata.home`` the runner reports, and the test skips with a clear reason
if the root somehow isn't under home (e.g. an unusual local layout).
"""

from __future__ import annotations

import gzip
import io
import json
import re
import shutil
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_ROOT_FILE = "README.md"
_README_CONTENT = "# Readme\n\nWorkspace-root file for the e2e link test.\n"
_AGENT_NAME = "filepath_links_demo"


def _agent_bundle(cwd: str) -> bytes:
    """Gzip-tar an agent YAML pinning ``os_env.cwd`` to ``cwd``.

    Mirrors the executor block of the conftest test agent so the strict
    validator accepts it; the model is never invoked (output is seeded via
    ``external_assistant_message``). The ``*.yaml`` arcname routes the bundle
    through the omnigent compat adapter, matching the conftest helpers.

    :param cwd: Absolute workspace directory the runner should use as root.
    :returns: ``.tar.gz`` bytes for multipart upload.
    """
    yaml_text = f"""\
name: {_AGENT_NAME}
prompt: You are a deterministic test assistant.

executor:
  model: databricks-gpt-5-4
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{_AGENT_NAME}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def linkify_session(
    live_server: str,
    runner_id: str,
) -> Iterator[tuple[str, str, str, str]]:
    """Bind a session whose workspace root is under ``$HOME`` and seed a reply.

    Creates a fresh workspace under ``Path.home()`` with a root-level
    ``README.md``, binds a session to the spawned runner, reads the live
    ``metadata.root`` / ``metadata.home``, derives a ``~/...`` and an absolute
    path pointing at that ``README.md``, then appends a deterministic assistant
    bubble mentioning them (plus ``/etc/hosts`` as the outside-workspace
    negative) via ``external_assistant_message`` so no LLM turn runs.

    :param live_server: Spawned server base URL.
    :param runner_id: Token-bound runner id to bind the session to.
    :returns: ``(base_url, session_id, tilde_path, abs_path)``.
    """
    # Under $HOME so the runner's default-env root is under home — the
    # precondition for "~" expansion to resolve inside the workspace.
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-ui-links-", dir=Path.home()))
    (ws / _ROOT_FILE).write_text(_README_CONTENT)

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _agent_bundle(str(ws)), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    try:
        patch_resp = httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        )
        patch_resp.raise_for_status()

        env_resp = httpx.get(
            f"{live_server}/v1/sessions/{session_id}/resources/environments/default",
            timeout=10.0,
        )
        env_resp.raise_for_status()
        metadata = env_resp.json().get("metadata", {})
        root = metadata.get("root")
        home = metadata.get("home")
        # ``home`` is the field the fix adds — its absence means pre-fix code.
        assert root, "environment must report metadata.root"
        assert home, "environment must report metadata.home (the field the fix adds)"
        # "~" expands to home, so the resolved path only lands inside the root
        # when root is under home. Pinning os_env.cwd under home should make
        # this hold; skip (don't fail) on an unusual layout where it doesn't.
        if not (root == home or root.startswith(f"{home}/")):
            pytest.skip(
                f"workspace root ({root!r}) is not under runner home ({home!r}); "
                "cannot exercise '~' expansion in this environment"
            )

        rel_from_home = root[len(home) :].lstrip("/")
        tilde_path = f"~/{rel_from_home}/{_ROOT_FILE}" if rel_from_home else f"~/{_ROOT_FILE}"
        abs_path = f"{root}/{_ROOT_FILE}"

        message_text = f"Files I referenced:\n\n- `{tilde_path}`\n- `{abs_path}`\n- `/etc/hosts`\n"
        event_resp = httpx.post(
            f"{live_server}/v1/sessions/{session_id}/events",
            json={
                "type": "external_assistant_message",
                "data": {"agent": _AGENT_NAME, "text": message_text},
            },
            timeout=10.0,
        )
        event_resp.raise_for_status()

        yield (live_server, session_id, tilde_path, abs_path)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


def test_chat_linkifies_workspace_paths_including_home_relative(
    page: Page,
    linkify_session: tuple[str, str, str, str],
) -> None:
    """Tilde + absolute workspace paths in a chat reply linkify; outside paths don't."""
    base_url, session_id, tilde_path, abs_path = linkify_session
    page.goto(f"{base_url}/c/{session_id}")

    # The home-relative span is a clickable link. Its accessible name is the
    # original text the agent wrote (``~/<root>/README.md``); a failure here
    # means either the runner didn't emit metadata.home or the frontend didn't
    # expand "~" / strip the root — the exact regression the bug report hit.
    tilde_link = page.get_by_role("button", name=tilde_path)
    expect(tilde_link).to_be_visible(timeout=30_000)

    # The absolute-under-root form resolves the same way (was rejected outright
    # before the fix because it starts with "/").
    expect(page.get_by_role("button", name=abs_path)).to_be_visible()

    # The negative: an absolute path outside the workspace must NOT be a link and
    # must remain an inert <code> span. A button here would mean we linkified a
    # path the FileViewer can't open.
    expect(page.get_by_role("button", name="/etc/hosts")).to_have_count(0)
    hosts_span = page.get_by_text("/etc/hosts", exact=True)
    expect(hosts_span).to_be_visible()
    assert hosts_span.evaluate("el => el.tagName") == "CODE", (
        "/etc/hosts should stay an inert <code> span, not a link — a different "
        "tag means an outside-workspace path was wrongly linkified."
    )

    # Clicking the tilde link opens the FileViewer on the RESOLVED relative path
    # (README.md), not the literal "~/..." text. The clinching assertion is the
    # URL: openFile writes the opened path to ``?file=<path>``, so a ``file=README.md``
    # query proves the link target was resolved to the workspace-relative form
    # (README.md lives only at the workspace root) rather than the raw ``~/...``.
    # We assert the URL (not FileViewer visibility) because the viewer mounts as
    # two testid="file-viewer" asides (mobile + desktop rail) and its slide-in
    # transition makes a ``.last`` + to_be_visible() check flaky in CI; the URL
    # is the deterministic signal of what this test actually verifies.
    tilde_link.click()
    page.wait_for_url(re.compile(r"[?&]file=README\.md(?:&|$)"), timeout=15_000)
    # Secondary confirmation the viewer mounted on the resolved file. to_contain_text
    # auto-waits on text content without requiring the flaky visibility state.
    expect(page.get_by_test_id("file-viewer").last).to_contain_text(_ROOT_FILE, timeout=15_000)
