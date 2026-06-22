"""
Unit tests for the REPL's tmux pane integration helpers.

The integration's job is to (a) advertise this REPL's pane as an
omnigent context source via custom pane options and (b) wrap the
user's prefix-table ``split-window`` / ``new-window`` bindings with
``if-shell -F`` conditionals that route to ``omnigent pane-split``
when the focused pane has ``@omnigent-conv-id`` set, and otherwise
run the user's exact original command.

These tests exercise the pure parsing / classification helpers and
verify the subprocess command shape produced by the registration
flow. The end-to-end "actually fires the chooser when prefix + " is
pressed in a real tmux" check lives in the manual verification step
documented in ``designs/REPL_TMUX_PANE_SPLIT.md`` § 6 phase 5.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.repl._tmux_pane import (
    OPT_AGENT_NAME,
    OPT_AGENT_YAML,
    OPT_CONV_ID,
    OPT_LAUNCH_ARGV,
    OPT_SERVER_URL,
    SplitBinding,
    _classify,
    _discover_split_bindings,
    _parse_bind_line,
    _wrap_binding,
    register_pane,
)

# ── shared subprocess.run stub helpers ───────────────────────


def _make_capturing_runner(captured: list[list[str]]) -> Any:
    """
    Build a ``subprocess.run``-shaped function that records each
    invocation's argv into *captured* and returns a stub result.

    Avoids the ``lambda cmd, **_: captured.append(cmd) or …`` hack
    that mypy flags (``list.append`` returns ``None``).
    """

    class _Result:
        returncode = 0
        stdout = ""

    def runner(cmd: list[str], **_: object) -> _Result:
        captured.append(cmd)
        return _Result()

    return runner


# ── _parse_bind_line ──────────────────────────────────────────


def test_parse_bind_line_default_quote_binding() -> None:
    """
    Default tmux 3.4 ships ``bind-key -T prefix \\" split-window -c
    "#{pane_current_path}"``. The parser must extract key=``"`` and
    command tokens=``['split-window', '-c', '#{pane_current_path}']``.

    Claim: parsing the default binding succeeds. A regression that
    drops the backslash-decoding (or breaks shlex token boundaries
    around the embedded ``#{...}``) would fail this — the parser
    is the seam every later step relies on.
    """
    line = 'bind-key -T prefix \\" split-window -c "#{pane_current_path}"'
    parsed = _parse_bind_line(line)
    assert parsed is not None
    key, tokens = parsed
    # ``\"`` decodes to the literal double-quote char.
    assert key == '"', f'expected key=", got {key!r}'
    assert tokens == ["split-window", "-c", "#{pane_current_path}"], (
        f"expected the full split-window command preserved as 3 tokens; got "
        f"{tokens!r}. If the format string ``#{{pane_current_path}}`` got "
        f"split, shlex isn't being given a properly quoted line."
    )


def test_parse_bind_line_user_pipe_binding() -> None:
    """
    A user binding ``bind-key '|' split-window -h`` (after our
    code adds the ``-T prefix`` flag in passing) must parse
    cleanly. Unlike the default, ``|`` doesn't need backslash
    escaping in tmux output.
    """
    line = "bind-key -T prefix | split-window -h"
    parsed = _parse_bind_line(line)
    assert parsed is not None
    key, tokens = parsed
    assert key == "|"
    assert tokens == ["split-window", "-h"]


def test_parse_bind_line_returns_none_when_shlex_raises() -> None:
    """
    A line that shlex can't tokenize (unbalanced quotes,
    interrupted output) must return ``None`` rather than
    propagating ``ValueError``.

    Claim: the parser swallows the shlex failure and degrades
    to "skip this line". A regression that let the exception
    propagate would crash ``_discover_split_bindings`` on any
    pathological line in ``list-keys`` output.
    """
    # Unterminated quote — shlex.split raises ValueError on this.
    line = 'bind-key -T prefix x split-window -c "unclosed'
    assert _parse_bind_line(line) is None


def test_lambda_block_line_does_not_produce_a_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Tmux's mouse-menu bindings (e.g. ``bind-key -T prefix
    MouseDown3Pane if-shell -F '...' { display-menu ... split-window
    ... }``) shlex-parse to a key=``MouseDown3Pane`` and a command
    starting with ``if-shell`` — NOT ``split-window`` /
    ``new-window``. The classifier must reject them so they don't
    get wrapped (which would mangle the user's mouse menu).

    Claim: the discovery walker returns no bindings when given a
    mouse-menu line. A regression that wrapped the line would
    rewrite the user's right-click menu into a chooser launcher.
    """
    # A real-shaped (but trimmed for the test) tmux 3.4 mouse-menu
    # line. The key is ``MouseDown3Pane``, the command starts with
    # ``if-shell``. Brace blocks DO survive shlex.split as separate
    # tokens; the classifier filters them by command name.
    fake_output = (
        "bind-key -T prefix MouseDown3Pane if-shell -F "
        "'#{||:#{mouse_any_flag}}' 'select-pane -t =' "
        "'display-menu -t = \"Horizontal Split\" h split-window'"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_: type("R", (), {"stdout": fake_output, "returncode": 0})(),
    )
    # Empty result: the lambda-style binding's command isn't
    # ``split-window`` / ``new-window`` at the top level, so the
    # classifier filtered it out.
    assert _discover_split_bindings() == []


def test_parse_bind_line_returns_none_for_non_bind_key_line() -> None:
    """
    Non-``bind-key`` lines (blank lines, headers, debug output)
    must return ``None``.
    """
    assert _parse_bind_line("") is None
    assert _parse_bind_line("Some other tmux output line") is None
    assert _parse_bind_line("set-option -g status on") is None


# ── _classify ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        # Default ``split-window -c "<path>"`` → vertical (no -h flag).
        (["split-window", "-c", "#{pane_current_path}"], "v"),
        # Explicit -v flag → vertical.
        (["split-window", "-v"], "v"),
        # Explicit -h flag → horizontal.
        (["split-window", "-h"], "h"),
        # -h with extra args → horizontal.
        (["split-window", "-h", "-c", "#{pane_current_path}"], "h"),
        # ``new-window`` → window/tab.
        (["new-window"], "w"),
        (["new-window", "-c", "#{pane_current_path}"], "w"),
    ],
)
def test_classify_returns_correct_direction(tokens: list[str], expected: str) -> None:
    """
    Direction classification must match tmux's flag semantics:
    ``-h`` is horizontal (panes side-by-side), default and ``-v``
    are vertical (stacked). ``new-window`` is a tab.

    Claim: every recognized binding produces the right direction
    code. A regression that swapped ``-h``/``-v`` or that didn't
    treat the flag-less form as vertical would mis-route the
    chooser to the wrong split direction.
    """
    assert _classify(tokens) == expected


@pytest.mark.parametrize(
    "tokens",
    [
        # Empty tokens → not classifiable.
        [],
        # Custom shell wrapper, not a top-level split-window.
        ["run-shell", "my-custom-split-script"],
        # Lambda chain or display-menu — not a split-window.
        ["display-menu", "..."],
        # ``kill-pane`` etc. — no direction.
        ["kill-pane"],
    ],
)
def test_classify_returns_none_for_non_split_commands(tokens: list[str]) -> None:
    """
    Anything that isn't a top-level ``split-window`` or
    ``new-window`` must classify as ``None`` (skip — leave the
    user's binding untouched).

    Claim: custom split wrappers, kill-pane, lambda blocks, and
    empty token lists all return ``None``. A regression that
    classified custom commands as splittable would silently
    rewrite them, breaking the user's exotic setups in non-omnigent
    panes too.
    """
    assert _classify(tokens) is None


# ── _wrap_binding ──────────────────────────────────────────


def test_wrap_binding_constructs_if_shell_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_wrap_binding`` must issue a ``tmux bind-key -T prefix
    <key> if-shell -F '<conv-id-truthy>' '<chooser-cmd>'
    '<original-cmd>'`` invocation. The ``<original-cmd>`` field is
    bit-identical to what the user had bound.

    This is the load-bearing call: get the if-shell argv shape
    right and the chooser routes correctly; get it wrong and
    either non-omnigent panes break (lost original) or
    omnigent panes never see the chooser.

    Claim: the subprocess is called with the exact 10-element
    argv specified in the design doc. A regression that swapped
    the true/false branch order, dropped ``-F``, or quoted the
    original command incorrectly would fail this test loudly.
    """
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> Any:
        captured.append(cmd)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)

    binding = SplitBinding(
        key='"',
        direction="v",
        original_command='split-window -c "#{pane_current_path}"',
    )
    _wrap_binding(binding, ["/venv/bin/omnigent"])

    # Single subprocess call: the bind-key invocation.
    assert len(captured) == 1, (
        f"_wrap_binding should invoke tmux exactly once; got {len(captured)} call(s): {captured!r}"
    )
    cmd = captured[0]
    # The argv structure pinned: tmux bind-key -T prefix <key>
    # if-shell -F <fmt> <true> <false>.
    assert cmd[:5] == ["tmux", "bind-key", "-T", "prefix", '"'], (
        f"prefix of the bind-key invocation regressed; got {cmd[:5]!r}. "
        f"This pins the table='prefix' and key='\"' targeting."
    )
    assert cmd[5:8] == ["if-shell", "-F", "#{?#{@omnigent-conv-id},1,0}"], (
        f"if-shell -F format-string regressed; got {cmd[5:8]!r}. The "
        f"format must check truthiness of the @omnigent-conv-id pane "
        f"option — anything else and the wrapper either always or never "
        f"routes to the chooser."
    )
    # True branch: the chooser. Must use the absolute
    # ``omnigent_bin`` path (tmux's run-shell inherits the
    # tmux server's PATH, which usually doesn't include the venv
    # bin/), and direction code 'v' must appear so the user's
    # vertical-split key opens a vertical split.
    assert cmd[8] == ("run-shell '/venv/bin/omnigent pane-split -v -p #{pane_id}'"), (
        f"chooser command regressed; got {cmd[8]!r}. If the path is "
        f'missing or relative, ``run-shell`` will exit 127 ("command '
        f"not found\") because the tmux server's PATH typically "
        f"doesn't include the venv ``bin/``."
    )
    # False branch: the user's original command, byte-for-byte
    # preserved. Must round-trip whatever ``shlex.join`` produced
    # from the original tokens.
    assert cmd[9] == 'split-window -c "#{pane_current_path}"', (
        f"original-command branch regressed; got {cmd[9]!r}. The user's "
        f"binding must be preserved exactly so non-omnigent panes get "
        f"identical behavior to before — that's what makes the global "
        f"prefix-table mutation behaviorally invisible."
    )


def test_wrap_binding_uses_horizontal_direction_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    For a horizontal-split binding (e.g. ``%`` bound to
    ``split-window -h``), the chooser command must use ``-h``.

    Claim: the direction code carried on ``SplitBinding`` flows
    through to the chooser argv. A regression that hardcoded
    ``-v`` would always open vertical splits regardless of which
    key the user pressed.
    """
    captured: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        _make_capturing_runner(captured),
    )
    _wrap_binding(
        SplitBinding(key="%", direction="h", original_command="split-window -h"),
        ["/venv/bin/omnigent"],
    )
    assert captured[0][8] == ("run-shell '/venv/bin/omnigent pane-split -h -p #{pane_id}'")


def test_wrap_binding_uses_window_direction_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    For ``new-window`` bindings (``c`` by default), the direction
    code ``-w`` must flow into the chooser command.
    """
    captured: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        _make_capturing_runner(captured),
    )
    _wrap_binding(
        SplitBinding(key="c", direction="w", original_command="new-window"),
        ["/venv/bin/omnigent"],
    )
    assert captured[0][8] == ("run-shell '/venv/bin/omnigent pane-split -w -p #{pane_id}'")


def test_wrap_binding_quotes_paths_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A venv path like ``/Users/me/My Project/.venv/bin/omnigent``
    has a space and would tokenize wrong without ``shlex.quote``.

    Claim: the wrapper escapes spaces in the binary path so the
    embedded shell command stays a single token. A regression
    that used naive f-string interpolation would produce
    ``run-shell '/Users/me/My Project/.venv/bin/omnigent pane-split …'``
    where shell-word boundaries split the path mid-way and the
    binding fails.
    """
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _make_capturing_runner(captured))
    _wrap_binding(
        SplitBinding(key="|", direction="v", original_command="split-window -h"),
        ["/Users/me/My Project/.venv/bin/omnigent"],
    )
    chooser = captured[0][8]
    # The chooser is shell-escaped (whole inner string passed via
    # ``shlex.quote``). Round-trip it back to argv via
    # ``shlex.split``: we expect ``["run-shell", "<inner>"]``,
    # then split the inner again to recover the program argv.
    # The path-with-space must survive both round-trips as a
    # single token.
    outer = shlex.split(chooser)
    assert outer[0] == "run-shell", f"chooser must start with run-shell: {outer!r}"
    inner_argv = shlex.split(outer[1])
    assert inner_argv[0] == "/Users/me/My Project/.venv/bin/omnigent", (
        f"path-with-space did not round-trip as a single shell token; "
        f"inner argv[0]={inner_argv[0]!r}, full inner={inner_argv!r}. If "
        f"the path got split, the wrapper produces a broken binding that "
        f"fires ``cd: /Users/me/My`` instead of running the omnigent "
        f"binary."
    )
    # The pane-split subcommand + direction also survive.
    assert inner_argv[1:4] == ["pane-split", "-v", "-p"], (
        f"argv after the path didn't round-trip: {inner_argv!r}"
    )


def test_wrap_binding_supports_python_m_fallback_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When :func:`_resolve_omnigent_argv` falls back to
    ``[sys.executable, "-m", "omnigent.cli"]`` (no resolvable
    binary on the PATH the running process inherited), the
    wrapper must embed the full three-element argv into the
    chooser shell command.

    Claim: every prefix element appears, in order, in the
    rendered chooser. A regression that only used the first
    element would launch ``/path/to/python pane-split …`` —
    Python with no module flag, which fails immediately.
    """
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _make_capturing_runner(captured))
    _wrap_binding(
        SplitBinding(key='"', direction="v", original_command="split-window"),
        ["/usr/bin/python3", "-m", "omnigent.cli"],
    )
    chooser = captured[0][8]
    # All three prefix tokens must appear, joined by spaces, BEFORE
    # the ``pane-split`` subcommand. If only the python path
    # appears, the fallback path was truncated.
    assert "/usr/bin/python3 -m omnigent.cli pane-split -v" in chooser, (
        f"python-m fallback prefix not propagated; got chooser={chooser!r}. "
        f"If ``-m omnigent.cli`` is missing, the wrapper would invoke "
        f"the bare python interpreter without telling it what to run."
    )


# ── _resolve_omnigent_argv ──────────────────────────────


def test_resolve_argv_uses_abspath_when_argv0_is_path_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``sys.argv[0]`` contains a path separator (e.g.
    ``./.venv/bin/omnigent`` or already absolute), the resolver
    abspath's it directly without consulting ``shutil.which``.

    Claim: the result is the absolute form of argv[0], length 1.
    A regression that always went through ``shutil.which``
    would lose user-supplied paths that aren't on the PATH the
    Python process inherited.
    """
    from omnigent.repl._tmux_pane import _resolve_omnigent_argv

    monkeypatch.setattr("sys.argv", ["/some/abs/path/omnigent", "run"])
    argv = _resolve_omnigent_argv()
    assert argv == ["/some/abs/path/omnigent"], (
        f"path-shaped argv0 should pass through to abspath; got {argv!r}"
    )


def test_resolve_argv_uses_which_when_argv0_is_bare_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``sys.argv[0]`` is a bare name like ``"omnigent"`` (the
    shell already resolved it via PATH but didn't pass the
    absolute path along), the resolver does its own
    :func:`shutil.which` lookup.

    Claim: the resolver ends up with the absolute path that
    ``which`` returned. Without this, every bare-name invocation
    would fall through to the python-m fallback even though a
    perfectly good binary is on PATH.
    """
    from omnigent.repl._tmux_pane import _resolve_omnigent_argv

    monkeypatch.setattr("sys.argv", ["omnigent", "run"])
    monkeypatch.setattr(shutil, "which", lambda name: "/resolved/bin/omnigent")
    argv = _resolve_omnigent_argv()
    assert argv == ["/resolved/bin/omnigent"]


def test_resolve_argv_falls_back_to_python_m_when_which_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When neither argv[0] inspection nor ``shutil.which`` can find a
    binary (degraded environment, sandboxed PATH, etc.), the
    resolver falls back to ``[sys.executable, "-m",
    "omnigent.cli"]`` — bulletproof because if Python is
    running this code, ``omnigent.cli`` is importable.

    Claim: the fallback is exactly three elements with the
    running interpreter and the correct module path. A regression
    that returned a bare name would propagate the 127 ("command
    not found") error the original bug report described.
    """
    from omnigent.repl._tmux_pane import _resolve_omnigent_argv

    monkeypatch.setattr("sys.argv", ["omnigent", "run"])
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr("sys.executable", "/path/to/python")
    argv = _resolve_omnigent_argv()
    assert argv == ["/path/to/python", "-m", "omnigent.cli"], (
        f"python-m fallback regressed; got {argv!r}. The fallback is "
        f"the only path that works in environments where the omnigent "
        f"binary isn't directly findable, so silent breakage here means "
        f"silent UX degradation everywhere."
    )


# ── _discover_split_bindings ──────────────────────────────


def test_discover_unwraps_existing_wrapper_to_recover_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``register_pane`` is invoked a SECOND time on a pane that
    already has our wrapper installed, ``tmux list-keys -T prefix``
    returns the wrapper itself — not the user's original binding.
    Discovery must peel the wrapper off and recover the original
    command from the false branch, then re-classify it. Without
    this, repeat registration silently leaves an out-of-date
    wrapper in place — the live regression that prompted this
    code path (a pre-fix bare-name wrapper survived all subsequent
    register_pane runs because the new code didn't recognize the
    wrapper as wrappable).

    Claim: an already-wrapped binding is rediscovered with its
    original direction code AND the user's original command in
    the ``original_command`` field. A regression that lost
    either would leave stale wrappers permanently installed on
    the user's tmux server.
    """
    fake_output = (
        # The exact wrapper shape ``_wrap_binding`` produces. The
        # false branch is the user's ``split-window -c
        # "#{pane_current_path}"`` original.
        'bind-key -T prefix \\" if-shell -F '
        '"#{?#{@omnigent-conv-id},1,0}" '
        "\"run-shell 'omnigent pane-split -v -p #{pane_id}'\" "
        '"split-window -c \\"#{pane_current_path}\\""'
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_: type("R", (), {"stdout": fake_output, "returncode": 0})(),
    )

    from omnigent.repl._tmux_pane import _discover_split_bindings

    bindings = _discover_split_bindings()
    assert len(bindings) == 1, (
        f"expected one rediscovered binding from a wrapped line; got "
        f"{bindings!r}. If 0, the unwrap path was skipped and stale "
        f"wrappers will linger; if >1, the unwrap accidentally split "
        f"the line into multiple bindings."
    )
    b = bindings[0]
    assert b.key == '"', f"key regressed: {b.key!r}"
    # The classifier looked at the UNWRAPPED original command —
    # ``split-window`` (no -h) → vertical.
    assert b.direction == "v", (
        f"direction code didn't carry through unwrap; got {b.direction!r}. "
        f"This means the next ``register_pane`` would reinstall a wrapper "
        f"with the wrong direction."
    )
    # The user's original command is preserved for the next wrapper's
    # else branch — bit-identical to what was bound before our first
    # register_pane ran.
    assert "split-window" in b.original_command, (
        f"original command not recovered: {b.original_command!r}"
    )


def test_register_pane_strips_existing_python_m_prefix_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    _pane_integration_enabled: None,
) -> None:
    """
    Register-twice scenario: the second invocation receives a
    launch_argv that ALREADY starts with
    ``[<python>, -m, omnigent.cli, ...]`` (because the first
    invocation normalized it and the user's REPL is now running
    via ``python -m omnigent.cli``). The second invocation must
    NOT re-prepend, which would produce a doubled
    ``-m omnigent.cli -m omnigent.cli`` and break the picker
    when it tries to ``os.execvp`` the duplicated argv.

    Claim: after a second ``register_pane`` call, the stored
    ``@omnigent-launch-argv`` has exactly one ``-m omnigent.cli``
    marker in the prefix — same shape as a first-time call.
    A regression that re-prepended the prefix would store a
    doubled form and crash the picker on subsequent splits.
    """
    monkeypatch.setenv("TMUX", "/tmp/dummy,1234,0")
    monkeypatch.setenv("TMUX_PANE", "%0")
    # Force the resolver into the python-m fallback path so
    # the test is deterministic.
    monkeypatch.setattr("sys.argv", ["unknown-name", "run"])
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr("sys.executable", "/p/python")

    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> Any:
        captured.append(cmd)
        if cmd[:2] == ["tmux", "-V"]:
            return type("R", (), {"stdout": "tmux 3.4\n", "returncode": 0})()
        if cmd[:3] == ["tmux", "list-keys", "-T"]:
            return type("R", (), {"stdout": "", "returncode": 0})()
        return type("R", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Simulate the second register_pane invocation: launch_argv is
    # ALREADY in [python, -m, omnigent.cli, run, ...] form —
    # what a pane-picker-launched REPL would see at boot.
    register_pane(
        conv_id="conv_x",
        agent_name="a",
        agent_yaml=None,
        launch_argv=["/p/python", "-m", "omnigent.cli", "run", "/x.yaml", "--omnigent"],
        server_url=None,
    )

    set_option_calls = [c for c in captured if c[:4] == ["tmux", "set-option", "-p", "-t"]]
    launch_call = next(c for c in set_option_calls if c[5] == OPT_LAUNCH_ARGV)
    stored_argv = json.loads(launch_call[6])
    assert stored_argv == [
        "/p/python",
        "-m",
        "omnigent.cli",
        "run",
        "/x.yaml",
        "--omnigent",
    ], (
        f"launch-argv regressed to a doubled form: {stored_argv!r}. The "
        f"picker calls ``os.execvp(argv[0], argv)``, so a doubled "
        f"``-m omnigent.cli`` would crash with a Python module-import "
        f"error or invoke a wrong subcommand."
    )


def test_register_pane_repairs_already_doubled_prefix(
    monkeypatch: pytest.MonkeyPatch,
    _pane_integration_enabled: None,
) -> None:
    """
    Regression test for the live state observed on a real pane:
    ``[python, -m, omnigent.cli, -m, omnigent.cli, run, ...]``
    — left there by an earlier register_pane that stripped only
    one ``-m omnigent.cli`` prefix and re-prepended a fresh
    one. The new normalization scans for the user's first
    subcommand (``run``) and slices from there, so any number
    of leading launcher tokens get collapsed to exactly one
    prefix.

    Claim: feeding a doubled (or n-tupled) prefix produces the
    correctly-shaped single-prefix output. A regression that
    only stripped one layer would leave the doubled state in
    place, perpetuating the bug across re-runs.
    """
    monkeypatch.setenv("TMUX", "/tmp/dummy,1234,0")
    monkeypatch.setenv("TMUX_PANE", "%0")
    monkeypatch.setattr("sys.argv", ["unknown-name", "run"])
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr("sys.executable", "/p/python")

    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> Any:
        captured.append(cmd)
        if cmd[:2] == ["tmux", "-V"]:
            return type("R", (), {"stdout": "tmux 3.4\n", "returncode": 0})()
        if cmd[:3] == ["tmux", "list-keys", "-T"]:
            return type("R", (), {"stdout": "", "returncode": 0})()
        return type("R", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Doubled form — exactly the shape the live pane had after
    # one buggy register_pane invocation chained onto a previous one.
    register_pane(
        conv_id="conv_x",
        agent_name="a",
        agent_yaml=None,
        launch_argv=[
            "/p/python",
            "-m",
            "omnigent.cli",
            "-m",
            "omnigent.cli",
            "run",
            "/x.yaml",
            "--omnigent",
            "--profile",
            "test-profile",
        ],
        server_url=None,
    )

    set_option_calls = [c for c in captured if c[:4] == ["tmux", "set-option", "-p", "-t"]]
    launch_call = next(c for c in set_option_calls if c[5] == OPT_LAUNCH_ARGV)
    stored_argv = json.loads(launch_call[6])
    assert stored_argv == [
        "/p/python",
        "-m",
        "omnigent.cli",
        "run",
        "/x.yaml",
        "--omnigent",
        "--profile",
        "test-profile",
    ], (
        f"doubled prefix not repaired; got {stored_argv!r}. The walker "
        f"must scan past every launcher token until it hits the user's "
        f"first subcommand (``run``), then re-prepend a single fresh "
        f"resolved prefix. If the doubled form survives, every future "
        f"register_pane keeps growing the prefix and eventually the "
        f"picker's ``os.execvp`` blows up."
    )


def test_discover_split_bindings_picks_default_three_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    On a stock tmux 3.4 ``list-keys -T prefix`` the parser should
    find exactly the three default split keys: ``"``, ``%``, and
    ``c``. Each gets the right direction.

    Claim: stock tmux produces ``[('"', 'v'), ('%', 'h'), ('c', 'w')]``
    (ordering matches whatever ``list-keys`` returned, which on
    3.4 is alphabetical). A regression that mis-classified one
    of these would change which split key opens which kind of
    new pane.
    """
    fake_output = "\n".join(
        [
            'bind-key -T prefix \\" split-window -c "#{pane_current_path}"',
            'bind-key -T prefix \\% split-window -h -c "#{pane_current_path}"',
            'bind-key -T prefix c new-window -c "#{pane_current_path}"',
            # Noise: a non-split binding that should be ignored.
            "bind-key -T prefix d detach-client",
        ]
    )

    def fake_run(cmd: list[str], **_: object) -> Any:
        # Only the list-keys probe is expected here.
        assert cmd[:3] == ["tmux", "list-keys", "-T"], cmd
        return type("R", (), {"stdout": fake_output, "returncode": 0})()

    monkeypatch.setattr(subprocess, "run", fake_run)

    bindings = _discover_split_bindings()
    keys_dirs = [(b.key, b.direction) for b in bindings]
    # 3 = the three default split bindings. ``d`` was filtered out
    # (not a split-window/new-window). If 2, one of the splits
    # was mis-classified or skipped; if 4, ``d`` leaked through.
    assert keys_dirs == [('"', "v"), ("%", "h"), ("c", "w")], (
        f"discovery regressed; expected default split key set, got "
        f"{keys_dirs!r}. If ``d`` is in there, the classifier accepted "
        f"a non-split-window binding. If shorter, one of the defaults "
        f"was mis-parsed."
    )


def test_discover_split_bindings_picks_user_custom_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Many users rebind splits to ``|`` (vertical) and ``-`` /
    ``_`` (horizontal). Discovery must mirror those custom keys
    so muscle memory works inside the omnigent pane.

    Claim: ``|`` and ``_`` get classified by the user's actual
    flag, not the key character. A regression that hardcoded
    ``-h`` based on key-name conventions would route ``|`` to
    the wrong direction.
    """
    fake_output = "\n".join(
        [
            "bind-key -T prefix | split-window -h",
            'bind-key -T prefix _ split-window -v -c "#{pane_current_path}"',
        ]
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_: type("R", (), {"stdout": fake_output, "returncode": 0})(),
    )
    bindings = _discover_split_bindings()
    assert [(b.key, b.direction) for b in bindings] == [
        ("|", "h"),
        ("_", "v"),
    ]


# ── register_pane ──────────────────────────────────────────


@pytest.fixture()
def _no_tmux(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the ``$TMUX`` env var to be unset for the duration."""
    monkeypatch.delenv("TMUX", raising=False)
    yield


@pytest.fixture()
def _pane_integration_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Flip ``PANE_INTEGRATION_ENABLED`` on for tests that exercise
    the wrapper-installation path.

    The constant defaults to ``False`` (kill-switch — feature is
    disabled by default in production). Tests that need the
    wrapper logic to actually run must explicitly enable it; this
    fixture makes the dependency obvious at the test signature.
    """
    monkeypatch.setattr("omnigent.repl._tmux_pane.PANE_INTEGRATION_ENABLED", True)
    yield


def test_register_pane_no_op_when_kill_switch_disabled_outside_tmux(
    _no_tmux: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Kill-switch + not inside tmux: zero subprocess calls.

    Claim: when ``PANE_INTEGRATION_ENABLED`` is False AND the
    REPL isn't running inside tmux, ``register_pane`` doesn't
    issue any tmux invocations. The active-cleanup branch only
    fires when we're inside tmux (we have a pane to clean up);
    outside tmux there's nothing to clean.
    """
    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _make_capturing_runner(captured))
    register_pane(
        conv_id="conv_x",
        agent_name="a",
        agent_yaml=None,
        launch_argv=["omnigent", "run"],
        server_url=None,
    )
    assert captured == [], f"kill-switch + outside-tmux must be a complete no-op; got {captured!r}"


def test_register_pane_unmarks_pane_when_kill_switch_disabled_inside_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Kill-switch ON (flag is False) + inside tmux: actively un-mark
    the pane so any leftover wrappers in the running tmux server
    fall through to the user's original split commands.

    Claim: ``register_pane`` issues exactly one ``set-option -u
    <opt>`` invocation per ``@omnigent-*`` option, targeting the
    current pane — and does NOT install any wrapper bindings,
    write new options, or run discovery. This is the cleanup path
    that makes "the feature is disabled" mean what users expect:
    pressing the split key in an omnigent pane stops opening
    the chooser, even if a prior flag-on run installed wrappers
    that are still in the running tmux server.
    """
    monkeypatch.setenv("TMUX", "/tmp/dummy,1234,0")
    monkeypatch.setenv("TMUX_PANE", "%5")

    captured: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _make_capturing_runner(captured))

    register_pane(
        conv_id="conv_x",
        agent_name="a",
        agent_yaml=None,
        launch_argv=["omnigent", "run"],
        server_url=None,
    )

    # Every captured call must be an unset of an ``@omnigent-*``
    # option targeting our pane. No bind-key, no list-keys, no
    # version probe — the kill-switch path skips all of those.
    for cmd in captured:
        assert cmd[:5] == ["tmux", "set-option", "-p", "-t", "%5"], (
            f"unexpected tmux call on kill-switch path: {cmd!r}. The "
            f"only thing this branch should do is unset @omnigent-* "
            f"options on the current pane."
        )
        assert "-u" in cmd, (
            f"set-option call without -u: {cmd!r}. The kill-switch "
            f"must UNSET options, not write new values."
        )
    unset_options = {cmd[6] for cmd in captured if cmd[5] == "-u"}
    # All five options must be unset. If any is missing, leftover
    # wrapper bindings could still find a truthy value on the pane
    # and route to the chooser — defeating the kill-switch.
    assert unset_options == {
        OPT_CONV_ID,
        OPT_AGENT_NAME,
        OPT_AGENT_YAML,
        OPT_LAUNCH_ARGV,
        OPT_SERVER_URL,
    }, (
        f"kill-switch must unset all 5 @omnigent-* options; got "
        f"{unset_options!r}. Missing options leave the pane "
        f"partially-marked and the wrapper's truthy check still "
        f"fires."
    )


def test_register_pane_no_op_outside_tmux(
    _no_tmux: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``$TMUX`` is unset, ``register_pane`` MUST NOT issue any
    subprocess calls — the REPL is running in a plain terminal
    and the integration is invisible.

    Claim: zero subprocess invocations. A regression that always
    invoked tmux would either crash (no tmux installed) or
    pollute the tmux server's state when run from a non-tmux
    terminal that happens to have a server running.
    """
    captured: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        _make_capturing_runner(captured),
    )
    register_pane(
        conv_id="conv_xyz",
        agent_name="test-agent",
        agent_yaml=Path("/tmp/x.yaml"),
        launch_argv=["omnigent", "run", "/tmp/x.yaml"],
        server_url="http://127.0.0.1:9000",
    )
    assert captured == [], (
        f"register_pane invoked tmux outside of a tmux session: "
        f"{captured!r}. Outside tmux it must be a complete no-op."
    )


def test_register_pane_skips_when_tmux_pane_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``$TMUX`` set but ``$TMUX_PANE`` unset is a degenerate
    environment (some sandbox / pseudo-tmux wrappers). Skip the
    registration — we can't identify which pane to mark.

    Claim: zero subprocess invocations even with $TMUX set, when
    $TMUX_PANE is missing.
    """
    monkeypatch.setenv("TMUX", "/tmp/dummy,1234,0")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    captured: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        _make_capturing_runner(captured),
    )
    register_pane(
        conv_id="conv_xyz",
        agent_name="test-agent",
        agent_yaml=None,
        launch_argv=["omnigent", "run"],
        server_url=None,
    )
    assert captured == []


def test_register_pane_advertises_options_and_wraps_bindings(
    monkeypatch: pytest.MonkeyPatch,
    _pane_integration_enabled: None,
) -> None:
    """
    Inside tmux with a recent-enough version, ``register_pane``
    must:

    1. Set every required pane option (conv-id, agent-name,
       agent-yaml, launch-argv, server-url).
    2. Invoke ``list-keys -T prefix`` once to discover bindings.
    3. Issue one ``bind-key`` wrapper per discovered split.

    The end-to-end shape is what the design doc § 5.2 documents
    and what the chooser depends on. A regression here would
    either skip pane-option setup (chooser can't read context)
    or skip wrapper installation (split key still does plain
    split, no chooser).

    Claim: ``@omnigent-launch-argv`` is JSON, all five options
    appear, and the wrapper invocation count equals the number of
    discovered split bindings (3 here).
    """
    monkeypatch.setenv("TMUX", "/tmp/dummy,1234,0")
    monkeypatch.setenv("TMUX_PANE", "%0")
    # Pin the resolver: argv0=bare name, ``which`` returns a known
    # absolute path. This is the path register_pane will splice
    # into ``launch_argv[0]`` and the wrapper's chooser command
    # so the assertions below can predict the exact stored value.
    monkeypatch.setattr("sys.argv", ["omnigent", "run", "/agents/cs.yaml", "--omnigent"])
    monkeypatch.setattr(shutil, "which", lambda name: "/venv/bin/omnigent")

    list_keys_output = "\n".join(
        [
            'bind-key -T prefix \\" split-window -c "#{pane_current_path}"',
            'bind-key -T prefix \\% split-window -h -c "#{pane_current_path}"',
            'bind-key -T prefix c new-window -c "#{pane_current_path}"',
        ]
    )

    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> Any:
        captured.append(cmd)
        # Specialize ``tmux -V`` and ``list-keys`` responses.
        if cmd[:2] == ["tmux", "-V"]:
            return type("R", (), {"stdout": "tmux 3.4\n", "returncode": 0})()
        if cmd[:3] == ["tmux", "list-keys", "-T"]:
            return type("R", (), {"stdout": list_keys_output, "returncode": 0})()
        return type("R", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(subprocess, "run", fake_run)

    register_pane(
        conv_id="conv_abc123",
        agent_name="coding-supervisor",
        agent_yaml=Path("/agents/cs.yaml"),
        launch_argv=["omnigent", "run", "/agents/cs.yaml", "--omnigent"],
        server_url="http://127.0.0.1:8123",
    )

    # Every pane option set.
    set_option_values: dict[str, str] = {}
    for cmd in captured:
        if cmd[:4] == ["tmux", "set-option", "-p", "-t"]:
            # set-option -p -t %0 <name> <value>
            set_option_values[cmd[5]] = cmd[6]
    assert set_option_values[OPT_CONV_ID] == "conv_abc123"
    assert set_option_values[OPT_AGENT_NAME] == "coding-supervisor"
    assert set_option_values[OPT_AGENT_YAML] == "/agents/cs.yaml"
    # launch-argv must round-trip through JSON, with argv[0] swapped
    # for the resolved absolute path so the picker's later
    # ``os.execvp`` works regardless of the new pane's PATH.
    parsed_argv = json.loads(set_option_values[OPT_LAUNCH_ARGV])
    assert parsed_argv == ["/venv/bin/omnigent", "run", "/agents/cs.yaml", "--omnigent"], (
        f"launch-argv[0] must be normalized to the resolved absolute "
        f"path so the picker's exec doesn't depend on tmux's PATH; got "
        f"{parsed_argv!r}. If argv[0] is still 'omnigent' (bare), the "
        f"picker will hit exit 127 when it tries to relaunch."
    )
    assert set_option_values[OPT_SERVER_URL] == "http://127.0.0.1:8123"

    # Three wrapper installations: one per discovered binding.
    bind_key_calls = [c for c in captured if c[:2] == ["tmux", "bind-key"]]
    # 3 = ``"`` + ``%`` + ``c`` from list_keys_output above. If 0,
    # discovery silently failed; if anything else, the classifier
    # got confused.
    assert len(bind_key_calls) == 3, (
        f"expected 3 wrapper installations (one per default split "
        f"binding); got {len(bind_key_calls)}: {bind_key_calls!r}"
    )
    # Each wrapper must follow the documented shape: bind-key -T
    # prefix <key> if-shell -F <fmt> <chooser> <original>.
    for call in bind_key_calls:
        # The targeted key table must always be ``prefix`` (post-
        # prefix lookup table), not ``root`` or some other.
        assert call[2:4] == ["-T", "prefix"], (
            f"wrapper installed in wrong key table: {call!r}. The "
            f"if-shell-F dispatch only fires post-prefix, so anywhere "
            f"other than the ``prefix`` table is a regression."
        )
        # The wrapper's conditional must be the ``@omnigent-conv-id``
        # truthy check — that's what keeps the wrapper inert in
        # non-omnigent panes.
        if_shell_idx = call.index("if-shell")
        assert call[if_shell_idx + 1] == "-F"
        assert call[if_shell_idx + 2] == "#{?#{@omnigent-conv-id},1,0}"


def test_register_pane_skips_on_old_tmux(
    monkeypatch: pytest.MonkeyPatch,
    _pane_integration_enabled: None,
) -> None:
    """
    tmux < 3.2 lacks the pane-scoped hook semantics the integration
    relies on. ``register_pane`` must log a warning and skip both
    pane-option setup and wrapper installation when the running
    tmux is too old.

    Claim: when ``tmux -V`` reports ``2.9``, exactly one
    subprocess call happens (the version probe), and zero
    set-option / bind-key calls follow.
    """
    monkeypatch.setenv("TMUX", "/tmp/dummy,1234,0")
    monkeypatch.setenv("TMUX_PANE", "%0")

    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> Any:
        captured.append(cmd)
        if cmd[:2] == ["tmux", "-V"]:
            return type("R", (), {"stdout": "tmux 2.9\n", "returncode": 0})()
        return type("R", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(subprocess, "run", fake_run)

    register_pane(
        conv_id="conv_abc",
        agent_name="agent",
        agent_yaml=None,
        launch_argv=["omnigent", "run"],
        server_url=None,
    )

    # 1 = the version probe only. If >1, registration didn't
    # short-circuit on the old version.
    assert len(captured) == 1, (
        f"old-tmux skip regressed: expected 1 subprocess call (version "
        f"probe), got {len(captured)}: {captured!r}. set-option / "
        f"bind-key invocations on old tmux would either fail loudly or "
        f"behave inconsistently with our design."
    )
    assert captured[0][:2] == ["tmux", "-V"]
