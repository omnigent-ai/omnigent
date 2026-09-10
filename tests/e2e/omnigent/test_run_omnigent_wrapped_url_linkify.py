"""
End-to-end regression test: CLI terminal links must not be
truncated when URLs wrap across multiple rows.

Journey (what a user does):

1. Run ``omnigent run`` in a terminal (120 columns here — the
   harness default).
2. The agent prints an HTTP(S) URL that is longer than the
   terminal is wide — in its assistant text, and in the
   tool-call summary line for a shell command containing the
   URL.
3. The assistant-text URL wraps onto several terminal rows;
   the tool-call summary is elided with ``…`` at the row edge.
4. The user clicks the URL on its first displayed row.

Expected: every displayed URL fragment points at the FULL
destination (query string and fragment included).

Observed bug: ``linkify_ansi`` runs AFTER the text has been
wrapped/elided for display, so the URL regex only sees the
first-row fragment. The OSC 8 hyperlink emitted for the first
row targets only that fragment (e.g.
``https://host/seg-path/seg-path/…s`` without the query or
fragment), continuation rows carry no hyperlink at all, and
the elided tool-call summary links to a garbage destination
that embeds the ``…`` ellipsis character.

What this test pins that the existing coverage doesn't:

  - ``tests/e2e/omnigent/test_run_omnigent_url_linkify.py``
    proves a SHORT URL (fits on one row) is OSC 8-wrapped
    end-to-end. It never wraps, so it can't see this bug.
  - This test uses URLs longer than the PTY width and asserts
    on the OSC 8 *destination* bytes — the exact URL a
    terminal opens when the user clicks — not merely that some
    envelope exists.

Assertion strategy: collect every OSC 8 opener
(``\\x1b]8;<params>;<target>ST``) in the PTY stream, then:

  - assistant text (streamed lines AND the final Rich
    markdown/replacement render): at least one hyperlink
    pointing at the stream URL's host was emitted, and every
    one of them targets the complete URL;
  - tool-call summary: every hyperlink pointing at the tool
    URL's host (if any — a fix may legitimately stop
    hyperlinking elided summaries) targets the complete URL,
    never a truncated/elided fragment.

On the buggy build both fail with the truncated destinations
reported verbatim; if linkify were removed entirely the
assistant-text check fails on the missing hyperlink.
"""

from __future__ import annotations

import io
import re
import shutil
import uuid
from pathlib import Path

from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
    submit_prompt,
)
from tests.e2e.omnigent.conftest import configure_mock_llm

# URLs deliberately longer than the 120-column PTY the harness
# spawns (see ``_DEFAULT_COLS`` in ``_pexpect_harness``), so the
# render paths must wrap/elide them. Distinctive hosts let the
# assertions attribute each emitted hyperlink to the render path
# that produced it. Both end in a query string + fragment so a
# truncated destination is unambiguous.
_TOOL_HOST = "elided-tool.example.com"
_TOOL_URL = f"https://{_TOOL_HOST}/" + "seg-path/" * 10 + "?query=complete&other=value#end"

_STREAM_HOST = "wrapped-stream.example.com"
_STREAM_URL = f"https://{_STREAM_HOST}/" + "seg-path/" * 10 + "?query=complete&other=value#end"

# Every OSC 8 opener in the stream: ``ESC ] 8 ; <params> ;
# <target> (BEL | ESC \)``. Group 1 is the hyperlink target —
# the destination a terminal opens on click.
_OSC8_OPEN_RE = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")

# Agent whose first turn runs a shell command containing the tool
# URL (tool-call summary render path) and whose second turn
# streams the stream URL as assistant text (streaming render path
# plus the final Rich markdown/replacement render).
_YAML_BODY = f"""\
name: wrapped_linkify_e2e_test
prompt: |
  Test URL linkification of long, wrapping URLs.

  1. Call sys_os_shell with command set to the literal string:
       echo Visit {_TOOL_URL} for docs

  2. Then reply with one sentence that contains the literal URL
     {_STREAM_URL} and end the turn with the literal text "DONE".

executor:
  model: mock-model
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""

_MODEL = "mock-model"
_HARNESS = "openai-agents"

_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 60.0
_RUN_COMPLETE_TIMEOUT = 120.0
_EXIT_TIMEOUT = 15.0


def test_run_omnigent_wrapped_url_links_full_destination(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    A URL that wraps across terminal rows must still hyperlink
    to its FULL destination on every row that displays it.

    Drives the real REPL under a PTY: turn 1 issues a shell
    tool call whose command contains the long tool URL (the
    REPL displays it as an elided tool-call summary), turn 2
    streams the long stream URL as assistant text (streamed
    lines, then the final Rich markdown/replacement render).
    Then inspects every OSC 8 hyperlink the CLI emitted and
    requires each one that points at a test host to carry the
    complete URL (query and fragment included) as its
    destination.

    :param omnigent_python: Path to the worktree venv's python.
    :param omnigent_repo_root: Repo root used as subprocess cwd.
    :param mock_credentials_env: Mock-LLM env for the subprocess.
    :param mock_llm_server_url: Mock server URL for scripting
        the two turns.
    :param tmp_path: Per-test pytest tmp dir for the agent YAML.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_shell_1",
                        "name": "sys_os_shell",
                        "arguments": f'{{"command": "echo Visit {_TOOL_URL} for docs"}}',
                    }
                ]
            },
            {"text": f"The full documentation lives at {_STREAM_URL} whenever you need it. DONE"},
        ],
    )
    yaml_path = tmp_path / "wrapped_linkify_e2e_test.yaml"
    yaml_path.write_text(_YAML_BODY)

    # Short top-level tmpdir — same macOS Unix-socket-path-length
    # workaround as the other REPL e2e tests.
    short_id = uuid.uuid4().hex[:6]
    test_tmpdir = Path("/tmp") / f"oa-wraplink-{short_id}"
    test_tmpdir.mkdir()
    try:
        env = dict(mock_credentials_env)
        env["TMPDIR"] = str(test_tmpdir)

        captured = io.StringIO()
        child = spawn_omnigent_run(
            omnigent_python=omnigent_python,
            yaml_path=yaml_path,
            model=_MODEL,
            harness=_HARNESS,
            env=env,
            cwd=omnigent_repo_root,
            timeout=_SPAWN_TIMEOUT,
        )
        # Mirror every PTY byte — including OSC 8 escapes — into
        # ``captured`` for the destination assertions.
        child.logfile_read = captured

        try:
            child.expect(r"❯ ", timeout=_BOOT_TIMEOUT)
            submit_prompt(child, "go")
            child.expect("DONE", timeout=_RUN_COMPLETE_TIMEOUT)
            clean_exit(child, timeout=_EXIT_TIMEOUT)
        finally:
            if not child.closed:
                child.close(force=True)

        output = captured.getvalue()
        _assert_full_destination(
            output,
            url=_STREAM_URL,
            host=_STREAM_HOST,
            render_path="assistant text (streamed lines + final Rich render)",
            require_link=True,
        )
        _assert_full_destination(
            output,
            url=_TOOL_URL,
            host=_TOOL_HOST,
            render_path="tool-call summary display",
            require_link=False,
        )
    except BaseException:
        # On failure, keep the tmpdir so the server log is
        # inspectable.
        print(f"\n[wraplink-e2e debug] tmpdir preserved at {test_tmpdir}")
        raise
    else:
        shutil.rmtree(test_tmpdir, ignore_errors=True)


def _assert_full_destination(
    captured: str,
    *,
    url: str,
    host: str,
    render_path: str,
    require_link: bool,
) -> None:
    """
    Assert every OSC 8 hyperlink pointing at *host* targets the
    complete *url*.

    Two checks:

    1. When *require_link* is True, at least one hyperlink
       pointing at *host* was emitted — otherwise linkify
       didn't run on this render path at all (a different
       regression, caught loudly here too). The tool-call
       summary path passes ``require_link=False`` because a
       legitimate fix may stop hyperlinking elided summaries
       entirely rather than linking them to the full URL.
    2. Every such hyperlink's destination equals *url* exactly.
       On the buggy build the first displayed row is linked to
       only its own fragment (the destination is cut at the
       wrap/elision point, losing the path tail, query, and
       fragment), so this reports the truncated destinations
       verbatim.

    :param captured: Full PTY stream from ``logfile_read``.
    :param url: The complete URL the agent printed.
    :param host: The URL's distinctive hostname, used to select
        the hyperlinks this render path emitted.
    :param render_path: Human-readable render-path name for
        failure messages.
    :param require_link: Whether the absence of any hyperlink
        for *host* is itself a failure.
    """
    targets = [t for t in _OSC8_OPEN_RE.findall(captured) if host in t]

    if require_link:
        assert targets, (
            f"No OSC 8 hyperlink pointing at {host!r} found in the "
            f"captured PTY output — the {render_path} path emitted the "
            f"URL without any hyperlink (or never emitted it). "
            f"Expected at least one OSC 8 destination for {url!r}.\n\n"
            f"Captured tail (last 4000 chars):\n{captured[-4000:]}"
        )

    truncated = sorted({t for t in targets if t != url})
    assert not truncated, (
        f"Wrapped-URL hyperlink(s) from the {render_path} path do "
        f"not target the full destination.\n\n"
        f"Expected every OSC 8 destination to be the complete "
        f"URL:\n  {url!r}\n\n"
        f"but found {len(truncated)} truncated/incorrect "
        f"destination(s):\n"
        + "".join(f"  {t!r}\n" for t in truncated)
        + "\nA terminal user clicking the displayed URL opens the "
        f"truncated destination above, losing the path tail, query "
        f"string, and fragment (URL detection ran after Rich/text "
        f"wrapping and elision, so the link was cut at the row "
        f"boundary)."
    )
