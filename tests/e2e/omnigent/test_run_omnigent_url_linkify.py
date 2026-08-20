"""
End-to-end test for OSC 8 hyperlink rendering under
``omnigent run``.

Drives a real REPL session: the agent calls ``sys_os_shell`` to
print a URL through bash, the REPL renders the tool-result
panel through ``TerminalHost.output``, the post-render
``linkify_ansi`` hook wraps the URL in OSC 8 escape bytes,
those bytes pass through the PTY into pexpect's capture
buffer, and the assertion finds them there.

Why this is the e2e gap that the unit + integration tests
don't close:

  - ``tests/frontends/sdk/test_linkify.py`` covers the regex
    + trailing-punct logic in isolation (pure function).
  - ``tests/frontends/sdk/test_terminal_host.py`` ::
    ``test_output_wraps_urls_in_osc_8_hyperlink`` covers the
    wiring inside ``TerminalHost.output`` (capsys-captured).
  - Neither exercises the full path: tool dispatch → tool
    result panel construction → ``host.output(Panel(...))`` →
    Rich render → linkify → real PTY write → terminal
    receives bytes. This test pins that whole chain.

What can break this without breaking the lower layers:

  - Someone refactors the REPL to render tool result panels
    via a path that bypasses ``TerminalHost.output`` (e.g. a
    direct ``console.print`` somewhere in the SDK that
    skips the linkify hook).
  - The PTY layer (prompt-toolkit's stdout proxy) eats the
    OSC 8 bytes before they reach the wire — possible if
    someone adds an ANSI sanitizer for "safety."
  - The Omnigent mode SSE bridge translates tool-result events into
    a renderable shape that bypasses Rich Panel entirely.

Failure mode: assertion fails on missing ``\\x1b]8;;`` bytes
in the captured PTY output, with the captured tail dumped for
diagnosis.
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

# A deliberately distinctive URL the agent will echo — picked
# so the OSC 8 byte sequence containing it can be unambiguously
# located in the captured PTY stream. Real-looking enough that
# the URL regex in ``_linkify`` accepts it (``https://`` prefix +
# valid host chars), but distinctive enough that a substring
# match doesn't false-positive on something else in the buffer.
_TEST_URL = "https://omni-linkify-e2e-test.example.com/path"

# OSC 8 envelope components — pinned here so the test fails
# loudly if the wire format drifts.
_OSC_OPEN = "\x1b]8;;"
_OSC_CLOSE = "\x1b\\"

# Agent that echoes the test URL through bash. Tool-output
# path is more deterministic than asking the LLM to print the
# URL verbatim in its assistant text — bash's stdout is
# byte-exact and lands in the tool result panel via
# ``host.output(Panel(...))``.
_YAML_BODY = f"""\
name: linkify_e2e_test
prompt: |
  Test the URL linkification feature.

  1. Call sys_os_shell with command set to the literal string:
       echo Visit {_TEST_URL} for docs

  2. Briefly acknowledge the result (one sentence) and end the
     turn with the literal text "DONE".

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

# Whole run usually takes 15–25 s (one tool call + one
# assistant response). 90 s is generous.
_SPAWN_TIMEOUT = 60.0
_BOOT_TIMEOUT = 60.0
_RUN_COMPLETE_TIMEOUT = 120.0
_EXIT_TIMEOUT = 15.0


def test_run_omnigent_url_linkify_emits_osc_8_in_pty(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    Verify that URL output from a tool result panel emerges
    from the PTY with OSC 8 hyperlink escapes around the URL.

    Strategy:

    1. Spawn ``omnigent run --omnigent`` via pexpect.
    2. Configure mock LLM to call ``sys_os_shell`` with the
       echo command, then acknowledge with DONE.
    3. Capture all PTY output via ``logfile_read`` (a
       ``StringIO``).
    4. After ``DONE`` is observed, scan the captured bytes for
       the exact OSC 8 envelope around ``_TEST_URL``.

    Asserts the precise byte sequence
    ``\\x1b]8;;{URL}\\x1b\\\\{URL}\\x1b]8;;\\x1b\\\\`` appears
    in the PTY output, AND that bare ``_TEST_URL`` does not
    appear OUTSIDE such an OSC 8 envelope (no unwrapped
    occurrences). Both assertions matter: the first proves
    linkify ran; the second proves it ran on every render
    site that emits the URL (catches "linkify wired in one
    place but not another").

    Path-length note: same workaround as the rate-limit-approval
    e2e test — pytest's ``tmp_path`` is too deeply nested for
    macOS Unix-socket paths, so we put the per-test workdir
    under ``/tmp/`` directly.

    :param omnigent_python: Path to the worktree's
        ``.venv/bin/python``.
    :param omnigent_repo_root: Repo root used as cwd so the
        subprocess imports this worktree's ``omnigent``
        package, not the editable-install one.
    :param mock_credentials_env: Mock-LLM env vars pointing at
        the mock server.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    :param tmp_path: Per-test pytest tmp dir, used only for
        the YAML file.
    """
    # Turn 1: LLM calls sys_os_shell to echo the URL.
    # Turn 2: LLM acknowledges with DONE.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_shell_1",
                        "name": "sys_os_shell",
                        "arguments": f'{{"command": "echo Visit {_TEST_URL} for docs"}}',
                    }
                ]
            },
            {"text": "The command output the URL. DONE"},
        ],
    )
    yaml_path = tmp_path / "linkify_e2e_test.yaml"
    yaml_path.write_text(_YAML_BODY)

    # Short top-level tmpdir so the Omnigent server's per-run
    # subdirs (incl. tmux socket paths if the agent ever
    # touched one) stay under macOS's 104-char Unix-socket
    # limit. Same shape as test_run_omnigent_terminal_idle.py.
    short_id = uuid.uuid4().hex[:6]
    test_tmpdir = Path("/tmp") / f"oa-linkify-{short_id}"
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
        # logfile_read mirrors every PTY byte into ``captured``,
        # including OSC 8 escapes — the assertion target.
        child.logfile_read = captured

        try:
            # Boot signal — same workaround as the other
            # e2e tests: bottom-toolbar status doesn't always
            # paint under pexpect, so we anchor on ``❯``.
            child.expect(r"❯ ", timeout=_BOOT_TIMEOUT)
            submit_prompt(child, "go")
            child.expect("DONE", timeout=_RUN_COMPLETE_TIMEOUT)
            clean_exit(child, timeout=_EXIT_TIMEOUT)
        finally:
            if not child.closed:
                child.close(force=True)

        _assert_url_was_linkified(captured.getvalue())
    except BaseException:
        # On failure, leave the tmpdir intact so the Omnigent server
        # log is inspectable.
        print(f"\n[linkify-e2e debug] tmpdir preserved at {test_tmpdir}")
        raise
    else:
        shutil.rmtree(test_tmpdir, ignore_errors=True)


def _assert_url_was_linkified(captured: str) -> None:
    """
    Assert ``_TEST_URL`` appears wrapped in OSC 8 envelope and
    does not appear unwrapped anywhere in the PTY output.

    Two independent assertions:

    1. The exact OSC 8 byte sequence around the URL appears at
       least once. Pins the wire format. If linkify ever
       changed how it builds the envelope, this fails.
    2. Every occurrence of the URL in the buffer is preceded
       by ``\\x1b]8;;``. Pins coverage — every render site
       that emits the URL must have run through linkify.

    :param captured: The full PTY byte stream from ``pexpect``'s
        ``logfile_read``. Includes Rich-rendered panels,
        streaming text, ANSI styling, and OSC 8 escapes if the
        linkify hook fired.
    """
    expected_envelope = f"{_OSC_OPEN}{_TEST_URL}{_OSC_CLOSE}{_TEST_URL}{_OSC_OPEN}{_OSC_CLOSE}"

    # Assertion 1: the OSC 8 envelope is present. This is the
    # load-bearing check. Failure means linkify_ansi wasn't
    # called on the path that emitted the URL.
    assert expected_envelope in captured, (
        f"Expected OSC 8 hyperlink envelope around the test URL in "
        f"the captured PTY output. Looked for "
        f"{expected_envelope!r}; not found.\n\n"
        f"Likely cause: someone removed the ``linkify_ansi`` call "
        f"from one of the print sites in ``TerminalHost``, OR a "
        f"new render path was added that bypasses ``output()``. "
        f"The unit + integration tests in "
        f"tests/frontends/sdk/test_linkify.py and "
        f"test_terminal_host.py may still pass — they don't "
        f"exercise the full PTY chain.\n\n"
        f"Captured tail (last 4000 chars):\n{captured[-4000:]}"
    )

    # Assertion 2: no BARE (un-enveloped) occurrences of the
    # URL anywhere in the PTY output. Each OSC 8 envelope
    # contains the URL TWICE — once as the link target inside
    # the opener (``\x1b]8;;<URL>\x1b\\``) and once as the
    # visible display text immediately after the opener's ST.
    # So total URL occurrences = 2 × envelope_count + bare,
    # and we want bare == 0.
    envelope_pattern = re.compile(re.escape(expected_envelope))
    envelope_count = len(envelope_pattern.findall(captured))
    url_count = captured.count(_TEST_URL)
    bare_count = url_count - 2 * envelope_count

    assert bare_count == 0, (
        f"Found {bare_count} bare (un-OSC-8-wrapped) occurrence(s) "
        f"of the test URL in the captured PTY output. "
        f"(Total URL occurrences: {url_count}; envelopes: "
        f"{envelope_count}; expected bare: 0 since each envelope "
        f"holds 2 URL occurrences.)\n\n"
        f"This is a coverage gap: linkify is wired at most call "
        f"sites but missed at least one. Audit "
        f"``sdks/ui/omnigent_ui_sdk/terminal/_host.py`` for "
        f"``print()`` / ``sys.stdout.write()`` calls that emit "
        f"rendered content without first running through "
        f"``linkify_ansi``.\n\n"
        f"Captured tail (last 4000 chars):\n{captured[-4000:]}"
    )
