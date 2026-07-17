"""
Tests for the macOS Seatbelt (``sandbox-exec``) sandbox backend.

Layers tested:

- **Resolver**: :meth:`SeatbeltSandboxBackend.resolve` produces the
  right :class:`SandboxPolicy` shape (RO-by-default cwd, allow-hidden
  defaulting, env-passthrough propagation).
- **Platform + binary gates**: explicit ``OSError`` when the host
  isn't macOS or ``sandbox-exec`` is missing.
- **Profile content**: :meth:`SeatbeltSandboxBackend.wrap_launcher_argv`
  emits ``["sandbox-exec", "-p", <profile>, *argv]`` where the
  profile string contains the documented section markers (default
  deny, system RO subpaths, cwd allow, scratch RW, dotfile mask,
  network rules).
- **Profile size cap**: the spawn-time fail-loud check fires when the
  generated profile exceeds :data:`_MAX_PROFILE_BYTES`.

The cross-platform behavioural assertions (cwd RO blocks writes,
scratch is RW, network deny, env stripping, dotfile masking via the
shared walker) live under :mod:`tests.inner.sandbox.test_sandbox_behavior`
and :mod:`tests.inner.sandbox.test_egress_e2e`. Those run against
whichever backend is active on the host; the assertions here are
seatbelt-only argv / profile-string shape checks that need no real
``sandbox-exec`` subprocess and skip cleanly on non-macOS CI shards
where the platform-gate assertions still hold.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import SandboxPolicy, with_denied_unix_sockets
from omnigent.inner.seatbelt_sandbox import (
    _DEFAULT_CWD_ALLOW_HIDDEN,
    _DEFAULT_READ_SUBPATHS,
    _SANDBOX_EXEC_PATH,
    _UNSAFE_WIDEN_ANCESTORS,
    SeatbeltSandboxBackend,
    _build_profile,
    _ensure_executable_visible,
    _interpreter_install_root,
    _per_user_dyld_cache_subpath,
    _quote,
    _resolve_root,
    _symlink_hop_literals,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_backend() -> SeatbeltSandboxBackend:
    """
    Construct a fresh backend instance for tests that need a bare
    backend object (without going through the registry singleton).

    :returns: A new :class:`SeatbeltSandboxBackend` instance.
    """
    return SeatbeltSandboxBackend()


def _make_policy(
    cwd: Path,
    *,
    allow_hidden: list[str] | None = None,
    write_roots: list[Path] | None = None,
    allow_network: bool = True,
    read_roots: list[Path] | None = None,
    egress_relay_port: int | None = None,
    egress_socket_path: str | None = None,
) -> SandboxPolicy:
    """
    Build a :class:`SandboxPolicy` directly without going through the
    resolver.

    Used in tests that want full control over policy fields without
    spec parsing or platform gates.

    :param cwd: Effective working directory for the helper.
    :param allow_hidden: Override for ``cwd_allow_hidden``; ``None``
        keeps the field as ``None`` (the profile builder then emits
        zero allowed dotfiles for the mask check).
    :param write_roots: Explicit write roots; defaults to ``[]``
        (cwd RO).
    :param allow_network: Whether to share host network.
    :param read_roots: Explicit read roots; defaults to ``None`` (only
        the default system subpaths are visible).
    :param egress_relay_port: When set together with
        ``egress_socket_path``, marks the policy as having an active
        egress proxy so the profile generator emits the loopback +
        Unix-socket allow rules.
    :param egress_socket_path: Filesystem path of the parent-side
        Unix socket the relay forwards to.
    :returns: A populated :class:`SandboxPolicy`.
    """
    del cwd
    return SandboxPolicy(
        backend_type="darwin_seatbelt",
        active=True,
        read_roots=read_roots,
        write_roots=write_roots if write_roots is not None else [],
        write_files=[],
        allow_network=allow_network,
        cwd_allow_hidden=allow_hidden,
        egress_relay_port=egress_relay_port,
        egress_socket_path=egress_socket_path,
    )


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def test_resolve_default_keeps_cwd_read_only() -> None:
    """
    ``write_paths`` omitted (the common case) leaves ``write_roots``
    empty so the SBPL profile contains no ``(allow file-write*
    (subpath cwd))`` for cwd. This is the seatbelt-specific "no
    surprise writes" default documented at
    :meth:`SeatbeltSandboxBackend.resolve`.

    Failure here means a future edit silently flipped the cwd to
    writable, which would surprise users who explicitly chose the
    seatbelt backend for tighter isolation.
    """
    if sys.platform != "darwin":
        pytest.skip("seatbelt resolver requires macOS host")
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="darwin_seatbelt"),
    )
    policy = backend.resolve(spec, Path.cwd())
    assert policy.backend_type == "darwin_seatbelt"
    assert policy.active is True
    assert policy.write_roots == [], (
        "seatbelt resolve() must default write_roots to [] (cwd RO). "
        "If non-empty here, the resolver is silently elevating cwd "
        "to writable — opposite of the documented default."
    )
    assert policy.read_roots is None  # No spec-supplied read_paths.


def test_resolve_write_paths_dot_makes_cwd_writable() -> None:
    """
    Setting ``write_paths: ["."]`` flips cwd to writable. This is the
    documented opt-in: an opt-in spec produces a write_root that
    matches cwd, which the profile builder turns into an
    ``(allow file-write* (subpath <cwd>))`` rule alongside the
    existing read allow.
    """
    if sys.platform != "darwin":
        pytest.skip("seatbelt resolver requires macOS host")
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="darwin_seatbelt", write_paths=["."]),
    )
    policy = backend.resolve(spec, Path.cwd())
    assert policy.write_roots == [Path.cwd().resolve(strict=False)]


def test_resolve_default_cwd_allow_hidden_is_dot_venv() -> None:
    """
    ``cwd_allow_hidden=None`` in the spec resolves to the documented
    default :data:`_DEFAULT_CWD_ALLOW_HIDDEN` (``[".venv"]``) on the
    policy. The profile builder consumes ``policy.cwd_allow_hidden``
    rather than reaching back into the spec, so this default has to
    land on the policy at resolve time.
    """
    if sys.platform != "darwin":
        pytest.skip("seatbelt resolver requires macOS host")
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="darwin_seatbelt"),
    )
    policy = backend.resolve(spec, Path.cwd())
    assert policy.cwd_allow_hidden == list(_DEFAULT_CWD_ALLOW_HIDDEN), (
        "Default allowlist drift — _DEFAULT_CWD_ALLOW_HIDDEN is "
        "the documented baseline; if this fails, either the constant "
        "moved or the resolver stopped substituting the default."
    )


def test_resolve_explicit_cwd_allow_hidden_overrides_default() -> None:
    """
    An explicit ``cwd_allow_hidden`` in the spec replaces the default
    entirely (no merge). This matches the Fail-Loud contract — the
    spec-self-containment rule says the spec is the source of truth,
    not a delta against an invisible default.
    """
    if sys.platform != "darwin":
        pytest.skip("seatbelt resolver requires macOS host")
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(
            type="darwin_seatbelt",
            cwd_allow_hidden=[".cache", ".npmrc"],
        ),
    )
    policy = backend.resolve(spec, Path.cwd())
    assert policy.cwd_allow_hidden == [".cache", ".npmrc"]


def test_resolve_env_passthrough_propagates_to_policy() -> None:
    """
    ``env_passthrough`` in the spec lands on the policy verbatim so
    the helper-spawn env builder can apply it. Distinct from
    ``cwd_allow_hidden`` which has a substituted default, this list
    has no default and a missing spec entry yields ``None``.
    """
    if sys.platform != "darwin":
        pytest.skip("seatbelt resolver requires macOS host")
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(
            type="darwin_seatbelt",
            env_passthrough=["AWS_PROFILE", "GITHUB_TOKEN"],
        ),
    )
    policy = backend.resolve(spec, Path.cwd())
    assert policy.env_passthrough == ["AWS_PROFILE", "GITHUB_TOKEN"]


def test_resolve_raises_on_non_darwin() -> None:
    """
    The resolver hard-errors on non-macOS hosts. The seatbelt backend
    requires the macOS Sandbox subsystem; there is no fallback path.
    """
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="darwin_seatbelt"),
    )
    with patch("omnigent.inner.seatbelt_sandbox.sys.platform", "linux"):
        with pytest.raises(OSError, match="only available on macOS"):
            backend.resolve(spec, Path.cwd())


def test_resolve_raises_when_sandbox_exec_missing() -> None:
    """
    If ``sandbox-exec`` is not on PATH, the resolver fails loud with
    an actionable message. The user explicitly chose
    ``darwin_seatbelt``; silent fallback to a different backend
    would be a Fail-Loud violation.
    """
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="darwin_seatbelt"),
    )
    with patch("omnigent.inner.seatbelt_sandbox.sys.platform", "darwin"):
        with patch("omnigent.inner.seatbelt_sandbox.shutil.which", return_value=None):
            with pytest.raises(OSError, match="sandbox-exec"):
                backend.resolve(spec, Path.cwd())


# ---------------------------------------------------------------------------
# wrap_launcher_argv shape
# ---------------------------------------------------------------------------


def _safe_helper_argv(tmp_path: Path) -> list[str]:
    """
    Construct a helper-argv whose ``argv[0]`` lives inside *tmp_path*
    so :func:`_ensure_executable_visible` returns ``[]`` (no widen
    attempt) regardless of where the test runner's ``sys.executable``
    actually lives.

    Without this helper the tests below would hit :class:`OSError`
    from the H1/H2/H3 unsafe-widen guard whenever the runner's
    interpreter lives under one of the
    :data:`_UNSAFE_WIDEN_ANCESTORS` (``/Users`` on a typical macOS
    dev layout, ``/home`` on Linux CI when uv places the managed
    Python under ``/home/runner/.local/share/uv/python/...``). Tests
    that care about argv shape need the interpreter co-located under
    cwd to bypass that guard cleanly.

    Implementation: a **regular file** (not a symlink). The widen
    check evaluates both the literal path (``argv[0]``) and the
    resolved path (``Path(argv[0]).resolve()``). A symlink to
    ``sys.executable`` would resolve to ``/Users/...`` on macOS dev
    or ``/home/runner/...`` on Linux CI — both unsafe — and the
    widen guard would refuse. A regular file resolves to itself
    under *tmp_path*, so both checks land under cwd and the guard
    passes on every host. None of the tests using this helper
    actually ``exec`` ``argv[0]``; they only assert on argv shape /
    profile contents / chdir behaviour.

    :param tmp_path: pytest's per-test temp directory; will host the
        venv-style stub interpreter.
    :returns: An argv whose ``argv[0]`` lives under *tmp_path*.
    """
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    interpreter = venv_bin / "python3"
    if not interpreter.exists():
        # Regular file (not a symlink to the real interpreter) so
        # ``resolve()`` stays under *tmp_path* and the unsafe-widen
        # guard doesn't fire on hosts where the real interpreter
        # lives under ``/home`` (Linux CI w/ uv-managed Python) or
        # ``/Users`` (macOS dev).
        interpreter.write_text("#!/usr/bin/env python3\n")
        interpreter.chmod(0o755)
    return [str(interpreter), "-m", "omnigent.inner.os_env", "helper", "X"]


def test_wrap_launcher_argv_starts_with_sandbox_exec_and_appends_inner_argv(
    tmp_path: Path,
) -> None:
    """
    The wrapped argv must begin with the absolute path to
    ``sandbox-exec`` plus ``-f <profile-path>`` (so
    :func:`subprocess.Popen` exec's the launcher) and end with the
    original command unchanged so sandbox-exec runs it under the
    on-disk profile.

    Two security invariants pinned here:

    - M5: profile is delivered via ``-f <file>``, not ``-p <inline>``,
      so the profile contents (cwd structure, dotfile mask, egress
      socket path) don't appear in ``ps aux`` for other users.
    - M6: ``argv[0]`` is the **absolute** path to ``sandbox-exec``,
      not a bare name, so the spawn doesn't go through ``$PATH``
      lookup at Popen time.

    Failure here means the wrap is structurally broken — Popen
    would either run the wrong binary or pass sandbox-exec flags to
    the helper.
    """
    import os as _os

    backend = _make_backend()
    policy = _make_policy(tmp_path, allow_hidden=[".venv"])
    helper_argv = _safe_helper_argv(tmp_path)
    argv = backend.wrap_launcher_argv(helper_argv, policy, tmp_path)
    assert _os.path.isabs(argv[0]), (
        f"sandbox-exec must be an absolute path, got {argv[0]!r}. "
        "Bare names go through $PATH lookup at Popen time which "
        "creates a small TOCTOU window for $PATH manipulation."
    )
    assert argv[0].endswith("/sandbox-exec")
    assert argv[1] == "-f", (
        f"expected '-f <file>' delivery for profile (hides contents from ps); got {argv[1]!r}."
    )
    profile_path = argv[2]
    assert _os.path.isfile(profile_path), f"profile path {profile_path!r} doesn't exist on disk."
    assert profile_path.endswith(".sb")
    # File mode should be 0600 so only the parent user can read.
    mode = _os.stat(profile_path).st_mode & 0o777
    assert mode == 0o600, (
        f"profile file mode is 0{mode:o}, expected 0600 so other "
        f"users can't read the profile contents (cwd structure, "
        f"dotfile mask paths, egress socket path)."
    )
    assert argv[3:] == helper_argv


def test_wrap_launcher_argv_chdir_is_ignored_for_seatbelt(tmp_path: Path) -> None:
    """
    ``sandbox-exec`` has no ``--chdir`` analogue; the wrap ignores
    the ``chdir`` parameter regardless of value. The helper does its
    own ``os.chdir`` from the JSON config — this test pins the
    contract so a future refactor doesn't accidentally turn
    ``chdir`` into a profile-shape difference.
    """
    backend = _make_backend()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    policy = _make_policy(tmp_path, allow_hidden=[".venv"])
    helper_argv = _safe_helper_argv(tmp_path)
    argv_no_chdir = backend.wrap_launcher_argv(helper_argv, policy, tmp_path, chdir=None)
    argv_with_chdir = backend.wrap_launcher_argv(helper_argv, policy, tmp_path, chdir=scratch)
    # argv[0] (sandbox-exec) and argv[3:] (helper argv) must match;
    # argv[2] (profile path) is a fresh tempfile per call so it
    # differs but the profile CONTENTS must be identical.
    assert argv_no_chdir[0] == argv_with_chdir[0]
    assert argv_no_chdir[1] == argv_with_chdir[1] == "-f"
    assert argv_no_chdir[3:] == argv_with_chdir[3:]
    assert Path(argv_no_chdir[2]).read_text() == Path(argv_with_chdir[2]).read_text(), (
        "chdir must be a no-op for seatbelt — the helper subprocess "
        "chdirs itself from its JSON config. Differing profile "
        "contents here mean the profile shape now depends on chdir, "
        "which would silently diverge from the bwrap path."
    )


def test_wrap_launcher_argv_profile_size_cap_fails_loud(tmp_path: Path) -> None:
    """
    A profile larger than :data:`_MAX_PROFILE_BYTES` fails the spawn
    with an :class:`OSError` carrying the actionable spec keys
    (``cwd_hidden_scan_max_entries``, ``cwd_hidden_scan_overflow``),
    instead of letting ``sandbox-exec`` reject the on-disk profile
    with an opaque error.

    The trip wire is a giant ``read_roots`` list whose combined SBPL
    rule text exceeds the cap.
    """
    backend = _make_backend()
    # Each ``(allow file-read* (subpath "/x/<N>"))`` rule is ~40
    # bytes; 8000 of them lands well past the 256 KiB cap.
    bloat = [Path("/nonexistent") / str(i) for i in range(8000)]
    policy = _make_policy(tmp_path, read_roots=bloat, allow_hidden=[".venv"])
    helper_argv = _safe_helper_argv(tmp_path)
    with pytest.raises(OSError, match="profile exceeds"):
        backend.wrap_launcher_argv(helper_argv, policy, tmp_path)


# ---------------------------------------------------------------------------
# Profile content assertions
# ---------------------------------------------------------------------------


def test_profile_starts_with_default_deny_baseline(tmp_path: Path) -> None:
    """
    The profile always opens with ``(version 1)`` and the
    ``(deny default (with no-log))`` baseline. Every allow rule that
    follows is additive on top of this baseline; flipping it would
    silently grant unrestricted access.
    """
    policy = _make_policy(tmp_path)
    profile = _build_profile(policy, tmp_path.resolve(strict=False))
    lines = profile.splitlines()
    assert lines[0] == "(version 1)"
    assert "(deny default" in lines[1], (
        f"Second line must carry the default-deny; got {lines[1]!r}. "
        "If allow-default leaked in here every other rule would be "
        "redundant and the sandbox would be open."
    )


def test_profile_includes_each_default_read_subpath(tmp_path: Path) -> None:
    """
    Every entry in :data:`_DEFAULT_READ_SUBPATHS` (``/usr``, ``/System``,
    ``/Library``, …) gets a corresponding
    ``(allow file-read* (subpath "<path>"))`` rule. These are the
    minimum system mounts the helper needs (dyld, libSystem, Python
    stdlib, system CA bundle); a missing one would make Python fail
    to start with an opaque dyld error.
    """
    policy = _make_policy(tmp_path)
    profile = _build_profile(policy, tmp_path.resolve(strict=False))
    for root in _DEFAULT_READ_SUBPATHS:
        expected = f'(allow file-read* (subpath "{root}"))'
        assert expected in profile, (
            f"Default RO subpath {root!r} missing from profile. "
            "If absent, the helper subprocess will fail to load "
            "libSystem / dyld and the spawn will die before "
            "activate_sandbox runs."
        )


def test_profile_emits_cwd_read_allow(tmp_path: Path) -> None:
    """
    The cwd is always granted ``file-read*`` via a ``subpath`` rule.
    Without this the helper can't even read its own config file.
    """
    policy = _make_policy(tmp_path)
    cwd = tmp_path.resolve(strict=False)
    profile = _build_profile(policy, cwd)
    expected = f'(allow file-read* (subpath "{cwd}"))'
    assert expected in profile


def test_profile_cwd_write_allow_only_when_write_root_matches(tmp_path: Path) -> None:
    """
    The ``(allow file-write* (subpath <cwd>))`` rule appears iff cwd
    is in ``write_roots``. Default policy (empty ``write_roots``)
    must NOT contain a cwd-write rule, mirroring the bwrap
    ``--ro-bind`` default.
    """
    cwd = tmp_path.resolve(strict=False)
    write_rule = f'(allow file-write* (subpath "{cwd}"))'

    # Default: cwd not writable.
    policy_ro = _make_policy(tmp_path)
    assert write_rule not in _build_profile(policy_ro, cwd), (
        "Default profile contains a cwd-write allow; the seatbelt "
        "backend documents cwd RO by default. Opt-in is via "
        'write_paths: ["."].'
    )

    # Opt-in: write_paths=["."] → cwd writable.
    policy_rw = _make_policy(tmp_path, write_roots=[cwd])
    assert write_rule in _build_profile(policy_rw, cwd)


def test_profile_no_explicit_home_deny(tmp_path: Path) -> None:
    """
    HOME isolation is achieved by the global ``(deny default)`` plus
    selective allows — NOT by an explicit ``(deny ... (subpath HOME))``.
    SBPL's deny-wins semantics would make a blanket HOME deny
    silently override any cwd / venv / read_paths allow under HOME
    (the common case), so this is a load-bearing invariant.

    A regression here (someone adds ``(deny ... HOME)`` thinking
    bwrap-style additive layering applies) would silently break the
    typical macOS layout where everything lives under
    ``/Users/<me>/``.
    """
    home = Path("~").expanduser()
    policy = _make_policy(tmp_path)
    profile = _build_profile(policy, tmp_path.resolve(strict=False))
    forbidden = f'deny file-read* file-write* (subpath "{home}")'
    assert forbidden not in profile, (
        f"Profile contains an explicit HOME deny ({forbidden!r}); "
        "the seatbelt backend MUST NOT emit one because SBPL deny-"
        "wins would override the cwd / venv allows when those live "
        "under HOME (the common case)."
    )


def test_profile_scratch_tmpdir_gets_read_and_write_allows(tmp_path: Path) -> None:
    """
    A ``write_root`` under the system tempdir is treated as the
    helper's scratch tmpdir and gets BOTH read and write allows.
    This is what makes ``$TMPDIR`` usable inside the sandbox.

    L2 (security): the path is canonicalised before emission so the
    kernel's canonicalised match (``/var/folders/...`` →
    ``/private/var/folders/...`` on macOS) hits our allow rule. A
    non-canonical literal in the profile would silently miss.
    """
    import tempfile

    sys_tmp = Path(tempfile.gettempdir())
    scratch = sys_tmp / "omnigent-test-scratch"
    policy = _make_policy(tmp_path, write_roots=[scratch])
    profile = _build_profile(policy, tmp_path.resolve(strict=False))
    canonical_scratch = str(scratch.resolve(strict=False))
    assert f'(allow file-read* (subpath "{canonical_scratch}"))' in profile, (
        "Scratch RO allow uses the un-canonicalised path; the kernel "
        "canonicalises /var/folders → /private/var/folders before "
        "matching, so a non-canonical literal silently misses."
    )
    assert f'(allow file-write* (subpath "{canonical_scratch}"))' in profile


def test_profile_emits_extra_read_roots(tmp_path: Path) -> None:
    """
    Spec-supplied ``read_paths`` show up as
    ``(allow file-read* (subpath "<path>"))`` rules. Without this
    the spec author's explicit RO grants wouldn't take effect.
    """
    extra = tmp_path / "extra"
    extra.mkdir()
    policy = _make_policy(tmp_path, read_roots=[extra.resolve(strict=False)])
    profile = _build_profile(policy, tmp_path.resolve(strict=False))
    expected = f'(allow file-read* (subpath "{extra.resolve(strict=False)}"))'
    assert expected in profile


def test_profile_network_section_for_allow_network_true_no_egress(
    tmp_path: Path,
) -> None:
    """
    ``allow_network=True`` and no egress rules → ``(allow network*)``
    is emitted so the helper sees the host's full network stack.
    """
    policy = _make_policy(tmp_path, allow_network=True)
    profile = _build_profile(policy, tmp_path.resolve(strict=False))
    assert "(allow network*)" in profile


def test_profile_network_section_for_allow_network_false_no_egress(
    tmp_path: Path,
) -> None:
    """
    ``allow_network=False`` with no egress → the default-deny handles
    the block; NO ``network`` allow rules are emitted. The profile
    instead carries a marker comment so a reader can see this was a
    deliberate "rely on (deny default)" decision rather than a bug.
    """
    policy = _make_policy(tmp_path, allow_network=False)
    profile = _build_profile(policy, tmp_path.resolve(strict=False))
    assert "(allow network*)" not in profile
    assert "(allow network-bind" not in profile
    assert "(allow network-outbound" not in profile
    assert "(allow network-inbound" not in profile


def test_profile_denies_unix_control_socket_after_allow_network(
    tmp_path: Path,
) -> None:
    """
    A denied AF_UNIX socket emits a ``(deny network-outbound (remote
    unix-socket (path-literal <realpath>)))`` rule, and it lands AFTER
    the broad ``(allow network*)``.

    SBPL is last-match-wins, so with ``allow_network=True`` the broad
    allow would otherwise let the pane ``connect(2)`` to the tmux
    control socket. The deny must come last to win. We assert the rule
    text uses the canonical realpath (the kernel canonicalises before
    matching, e.g. ``/var`` → ``/private/var``) and that its line index
    is greater than the allow's.
    """
    sock = tmp_path / "inst" / "tmux.sock"
    policy = _make_policy(tmp_path, allow_network=True)
    policy = with_denied_unix_sockets(policy, [sock])

    profile = _build_profile(policy, tmp_path.resolve(strict=False))
    lines = profile.splitlines()

    canonical = str(Path(sock).resolve(strict=False))
    deny_rule = f'(deny network-outbound (remote unix-socket (path-literal "{canonical}")))'
    assert deny_rule in lines, f"missing socket deny rule; profile was:\n{profile}"
    assert "(allow network*)" in lines
    assert lines.index(deny_rule) > lines.index("(allow network*)"), (
        "socket deny rule must follow (allow network*) — SBPL last-match-wins, "
        "so a deny emitted before the broad allow would be overridden and the "
        "tmux control socket would stay reachable."
    )


def test_profile_no_unix_socket_deny_when_list_empty(tmp_path: Path) -> None:
    """
    With no ``deny_unix_socket_paths`` the profile emits no
    ``unix-socket`` deny rule — the containment is opt-in and must not
    appear for ordinary sandboxes (a stray deny could break a
    legitimate egress unix-socket allow).
    """
    policy = _make_policy(tmp_path, allow_network=True)
    assert policy.deny_unix_socket_paths is None

    profile = _build_profile(policy, tmp_path.resolve(strict=False))

    assert "(deny network-outbound (remote unix-socket" not in profile


def test_profile_network_section_for_active_egress_emits_narrow_allows(
    tmp_path: Path,
) -> None:
    """
    When ``policy.egress_relay_port`` and ``policy.egress_socket_path``
    are set, the profile emits the documented quadruple:

    - ``(allow network-bind (local ip "localhost:<port>"))``
    - ``(allow network-inbound (local ip "localhost:<port>"))``
    - ``(allow network-outbound (remote ip "localhost:<port>"))``
    - ``(allow network-outbound (remote unix-socket (path-literal
      "<realpath socket>")))``

    AND the broad ``(allow network*)`` is NOT emitted (egress mode
    must always be narrower than the ``allow_network=true`` mode).

    All four rules are load-bearing: bind without inbound silently
    fails listen() with EPERM, outbound-ip without outbound-unix
    silently fails the Unix-socket connect, and the un-canonicalised
    socket path doesn't match the kernel's canonicalised AF_UNIX
    target (``/var/folders`` → ``/private/var/folders``).
    """
    import tempfile

    sys_tmp = Path(tempfile.gettempdir())
    scratch = sys_tmp / "omnigent-egress-test-scratch"
    socket_path = scratch / ".egress.sock"
    policy = _make_policy(
        tmp_path,
        allow_network=True,  # ignored when egress is active
        write_roots=[scratch],
        egress_relay_port=18080,
        egress_socket_path=str(socket_path),
    )
    profile = _build_profile(policy, tmp_path.resolve(strict=False))

    assert '(allow network-bind (local ip "localhost:18080"))' in profile, (
        "Missing network-bind allow for the relay's loopback bind; "
        "the relay's bind() will fall through to the default deny."
    )
    assert '(allow network-inbound (local ip "localhost:18080"))' in profile, (
        "Missing network-inbound allow; bind succeeds but listen() "
        "returns EPERM and the relay never serves."
    )
    assert '(allow network-outbound (remote ip "localhost:18080"))' in profile, (
        "Missing network-outbound allow for HTTP clients in the helper."
    )
    canonical_socket = str(Path(str(socket_path)).resolve(strict=False))
    assert (
        f'(allow network-outbound (remote unix-socket (path-literal "{canonical_socket}")))'
        in profile
    ), (
        "Missing network-outbound allow on the canonical Unix-socket "
        "path. Note the path MUST be the realpath — kernel "
        "canonicalises /var/folders → /private/var/folders before "
        "matching, and an un-canonicalised rule silently misses."
    )
    assert "(allow network*)" not in profile, (
        "Active egress profile emitted (allow network*); egress mode "
        "MUST be narrower than allow_network=True so direct TCP "
        "bypass attempts fail at the syscall layer."
    )


def test_profile_dotfile_mask_uses_deny_rules(tmp_path: Path) -> None:
    """
    Top-level dotfiles not in ``cwd_allow_hidden`` are masked with
    per-path ``(deny file-read* file-write* (literal | subpath
    "<path>"))`` rules AFTER the cwd subpath allow. SBPL deny-wins
    means these per-path denies override the broad cwd allow exactly
    where we need them to. ``literal`` is used for files, ``subpath``
    for directories so the entire subtree is masked.
    """
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".venv").mkdir()  # on the default allowlist

    cwd = tmp_path.resolve(strict=False)
    policy = _make_policy(tmp_path, allow_hidden=[".venv"])
    profile = _build_profile(policy, cwd)

    env_deny = f'(deny file-read* file-write* (literal "{cwd / ".env"}"))'
    aws_deny = f'(deny file-read* file-write* (subpath "{cwd / ".aws"}"))'
    venv_deny_literal = f'(deny file-read* file-write* (literal "{cwd / ".venv"}"))'
    venv_deny_subpath = f'(deny file-read* file-write* (subpath "{cwd / ".venv"}"))'

    assert env_deny in profile, (
        ".env file not masked with a literal deny — the dotfile "
        "mask either skipped it or used the wrong rule form. SBPL "
        "deny-wins is the mechanism that lets per-path denies "
        "override the cwd allow, so the rule MUST be present and "
        "MUST follow the cwd subpath allow."
    )
    assert aws_deny in profile, (
        ".aws directory not masked with a subpath deny — a literal "
        "deny would only block the directory itself, not files "
        "underneath it (so .aws/credentials would still be readable)."
    )
    assert venv_deny_literal not in profile, (
        ".venv is on the allowlist but a deny rule landed for it; the allowlist filter regressed."
    )
    assert venv_deny_subpath not in profile, (
        ".venv is on the allowlist but a deny rule landed for it; the allowlist filter regressed."
    )

    # Ordering: the dotfile deny rules must appear AFTER the cwd
    # allow, otherwise the cwd allow could (in principle) re-grant
    # access — SBPL evaluates rules independently for matching, but
    # the documented intent is "mask wins over cwd", reflected in
    # rule order.
    cwd_allow_idx = profile.index(f'(allow file-read* (subpath "{cwd}"))')
    deny_idx = profile.index(env_deny)
    assert deny_idx > cwd_allow_idx, (
        "Dotfile deny rule appears before the cwd allow; the "
        "intended profile order is cwd-allow first, deny mask "
        "after, even though SBPL deny-wins doesn't depend on order."
    )


# ---------------------------------------------------------------------------
# Ancestor traversal (realpath() / lstat() walks)
#
# Regression coverage for the bug where Python's interpreter startup
# called ``realpath(sys.executable)`` and the kernel denied the
# parent-component walk under the default-deny because
# ``(allow file-read* (subpath cwd))`` covers the cwd subtree but NOT
# the strict ancestors above cwd (``/Users``, ``/Users/<me>``, …).
#
# The bug was masked by the existing test setup: pytest's ``tmp_path``
# resolves to ``/private/var/folders/.../pytest-XXX`` whose ancestors
# ARE covered by the ``/private/var/folders`` default subpath, AND
# the test runner's helper interpreter lives at
# ``/Users/<me>/repo/.venv/bin/python3`` which lies outside the tmp
# cwd, so :func:`_ensure_executable_visible` incidentally added a
# broad ``(allow file-read* (subpath "/Users"))`` rule for executable
# visibility. That broad allow ALSO satisfied the realpath ancestor
# walk, hiding the missing-ancestor-traversal bug from every test.
#
# In production the venv lives UNDER cwd, no broad ``/Users`` rule
# is added, and Python fails to start with
# ``python3: realpath: <cwd>/.venv/bin/: Operation not permitted``.
#
# These tests cover the gap directly:
#
# 1. Unit assertion that the ancestor-traversal block IS emitted for
#    a venv-under-cwd setup.
# 2. Unit assertion that the block uses the narrow
#    ``file-read-metadata`` permission (``stat`` only — not
#    ``file-read*`` which would also grant directory listing) so
#    the leak is bounded to "this parent exists".
# 3. End-to-end spawn test under ``sandbox-exec`` with cwd under
#    the real ``$HOME`` (instead of ``tmp_path``). This is the only
#    layer that catches the bug the way the user hit it.
# ---------------------------------------------------------------------------


def test_profile_emits_stat_only_ancestor_allows_for_cwd_under_home(
    tmp_path: Path,
) -> None:
    """
    Production-shape cwd (a path whose strict ancestors are not
    covered by any default RO subpath) gets a
    ``(allow file-read-metadata (literal <ancestor>))`` rule per
    uncovered ancestor.

    Without this, ``realpath()`` walks fail at the first uncovered
    component and Python aborts during ``Py_InitializeFromConfig``
    before the helper boots. ``file-read-metadata`` is the narrow
    ``stat``-only permission — strictly weaker than ``file-read*``
    on a subpath, so the only thing leaked is "this parent directory
    exists" (e.g. ``/Users``, ``/Users/<me>``).

    The test fakes a cwd at ``/Users/regression-test-XXX/repo`` so
    the assertion holds regardless of who runs the test or which
    venv layout they use; the rules are emitted purely from the
    cwd path string. ``_build_profile`` does not stat the cwd so
    the fake path is fine.
    """
    fake_cwd = Path("/Users/regression-test-XXX/repo")
    policy = _make_policy(fake_cwd)
    profile = _build_profile(policy, fake_cwd)
    for ancestor in ("/Users", "/Users/regression-test-XXX"):
        expected = f'(allow file-read-metadata (literal "{ancestor}"))'
        assert expected in profile, (
            f"Missing ancestor-traversal allow for {ancestor!r}. "
            "Python's realpath(sys.executable) on macOS lstat()'s "
            "every parent component of the executable path; an "
            "uncovered ancestor returns EPERM and the helper aborts "
            "before activate_sandbox runs."
        )
    forbidden_strong = '(allow file-read* (literal "/Users"))'
    assert forbidden_strong not in profile, (
        "Ancestor traversal allow widened from file-read-metadata "
        "to file-read*; that grants directory listing on /Users "
        "(every other user's home appears in readdir output)."
    )
    forbidden_subpath = '(allow file-read* (subpath "/Users"))'
    assert forbidden_subpath not in profile, (
        "Ancestor traversal allow widened from a per-ancestor "
        "literal to a (subpath /Users) — that grants read access to "
        "every other user's home directory, every other project, "
        "every dotfile, defeating the whole HOME deny-by-default."
    )


def test_profile_skips_ancestor_allows_already_covered_by_default_subpaths(
    tmp_path: Path,
) -> None:
    """
    When the ancestor walker finds a path that's already covered by
    a default RO subpath (``/usr``, ``/System``, ``/opt``, …), it
    must skip emitting a redundant ``file-read-metadata`` allow for
    that path — the existing subpath rule already covers traversal
    by containment.

    Pre-S1 hardening this test used ``/private/var/folders`` as the
    canonical example because tmp_path lives there and the broad
    ``/private/var/folders`` subpath was in
    :data:`_DEFAULT_READ_SUBPATHS`. After S1 narrowed that allow to
    the per-user dyld cache only, ``/private/var/folders`` is no
    longer a default and the walker correctly DOES emit a metadata
    allow for it (so the kernel can stat the cwd chain). The
    invariant the test pins is still "skip redundant ancestors",
    so we use ``/usr`` instead — anything under ``/usr`` does NOT
    need a per-ancestor metadata allow.
    """
    # Use a cwd under ``/usr`` (a real default RO subpath) so the
    # walker has an opportunity to redundantly emit metadata for
    # ``/usr`` itself, and assert it doesn't.
    fake_cwd = Path("/usr/local/test-cwd")
    policy = _make_policy(fake_cwd)
    profile = _build_profile(policy, fake_cwd)
    redundant = '(allow file-read-metadata (literal "/usr"))'
    assert redundant not in profile, (
        "Ancestor-traversal block emitted a metadata allow for "
        "/usr even though /usr is already in _DEFAULT_READ_SUBPATHS "
        "as a broad file-read* subpath. The walker should skip "
        "ancestors covered by an existing subpath rule — emitting "
        "both is noise in the profile."
    )


def test_ensure_executable_visible_does_not_widen_when_venv_is_under_cwd() -> None:
    """
    When the helper interpreter (``sys.executable``) lives UNDER
    cwd (the canonical production shape: ``~/proj/.venv/bin/python3``
    with cwd ``~/proj``), :func:`_ensure_executable_visible` MUST
    return ``[]`` — no broad ``(subpath /Users)`` rule may be
    added.

    This guards against a fix-the-symptom regression: someone hits
    the realpath-EPERM bug, mistakenly "fixes" it by always adding
    the topmost ancestor of ``sys.executable`` to ``extra_read_paths``,
    and silently re-introduces the broad ``/Users`` allow that
    masked the bug in tests in the first place. The narrow fix is
    per-ancestor ``file-read-metadata`` (covered by
    :func:`test_profile_emits_stat_only_ancestor_allows_for_cwd_under_home`),
    NOT a broader subpath here.
    """
    cwd = Path("/Users/me/proj")
    venv_python = cwd / ".venv" / "bin" / "python3"
    argv = [str(venv_python), "-m", "omnigent.inner.os_env", "helper"]
    extras = _ensure_executable_visible(argv, cwd)
    assert extras == [], (
        f"Got extra_read_paths={extras!r} for a venv UNDER cwd; "
        "expected []. Adding /Users here re-introduces the broad "
        "filesystem allow that masked the realpath-EPERM bug from "
        "every test (because the broad rule incidentally satisfied "
        "the realpath ancestor walk). The narrow fix is per-"
        "ancestor file-read-metadata, not a broader subpath here."
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_helper_boots_when_cwd_lives_under_home_regression() -> None:
    """
    End-to-end regression: spawn a real ``sandbox-exec`` subprocess
    in the production shape — cwd under ``$HOME`` AND the helper
    interpreter living UNDER cwd (i.e. ``cwd/.venv/bin/python3``).
    Python startup must succeed and the subprocess must reach user
    code. This is the only test layer that catches the
    realpath-EPERM bug the way the user hit it.

    Two test-harness artifacts hid this bug for the entire feature
    branch — this test reproduces production by undoing both:

    1. **pytest's ``tmp_path`` lives under ``/private/var/folders``**,
       whose ancestors are covered by the default
       ``/private/var/folders`` subpath. ``realpath()`` walks the
       chain ``/`` → ``/private`` → ``/private/var`` →
       ``/private/var/folders`` and every step has an ancestor allow
       by accident. Real cwds under ``$HOME`` don't get that. → Fix:
       use a directory directly under ``Path.home()``.

    2. **The test runner's ``sys.executable`` lives at
       ``~/repos/.../.venv/bin/python3``** — i.e. OUTSIDE pytest's
       ``tmp_path``. :func:`_ensure_executable_visible` notices this
       and emits a broad ``(allow file-read* (subpath /Users))``
       rule for interpreter visibility, which ALSO satisfies the
       realpath ancestor walk by accident. In production the venv
       lives UNDER cwd, that broad rule is NOT emitted, and the
       bug surfaces. → Fix: symlink the test runner's interpreter
       into ``<cwd>/.venv/bin/python3`` and exec via that path so
       ``_ensure_executable_visible`` returns ``[]``.

    With both artifacts removed, removing the ancestor-traversal
    block from :func:`_build_profile` makes this test fail with
    ``python3: realpath: <cwd>/.venv/bin/: Operation not permitted``
    — exactly the production failure mode.
    """
    import shutil
    import subprocess
    import uuid

    if shutil.which("sandbox-exec") is None:
        pytest.skip("sandbox-exec not on PATH")

    home = Path.home()
    cwd = home / f".omnigent-realpath-regression-{uuid.uuid4().hex}"
    (cwd / ".venv" / "bin").mkdir(parents=True)
    try:
        # Symlink the test runner's interpreter under cwd. To
        # avoid having the kernel follow a chain that exits cwd
        # (sys.executable usually lives at
        # ``~/repos/.../.venv/bin/python3`` which is itself a
        # symlink to ``/opt/homebrew/...``), point our symlink
        # directly at the fully-resolved real binary. That target
        # lives under ``/opt`` which is covered by the default
        # RO subpath, so the symlink-follow chain stays inside
        # paths the sandbox allows.
        #
        # The literal exec path is now ``<cwd>/.venv/bin/python3``,
        # which :func:`_ensure_executable_visible` classifies as
        # "inside cwd" and therefore emits NO extra interpreter-
        # visibility allows. This matches the production shape (uv-
        # managed venv at the project root) exactly.
        helper_python = cwd / ".venv" / "bin" / "python3"
        helper_python.symlink_to(Path(sys.executable).resolve(strict=True))

        backend = _make_backend()
        # ``allow_hidden=[".venv"]`` so the dotfile mask walker
        # doesn't deny our symlinked interpreter. This matches the
        # default ``_DEFAULT_CWD_ALLOW_HIDDEN`` that production
        # uses (``(".venv",)``); _make_policy defaults to ``None``
        # which the mask treats as an empty allowlist.
        policy = _make_policy(cwd.resolve(strict=False), allow_hidden=[".venv"])
        argv = [
            str(helper_python),
            "-c",
            "import sys; print('HELLO', sys.executable)",
        ]
        wrapped = backend.wrap_launcher_argv(argv, policy, cwd.resolve(strict=False), chdir=None)

        # M5: profile is now in a tempfile pointed to by wrapped[2]
        # (``-f <path>``) rather than inline in argv (``-p <text>``).
        # Read the file off disk for the content sanity-checks.
        assert wrapped[1] == "-f"
        profile = Path(wrapped[2]).read_text()
        assert '(allow file-read* (subpath "/Users"))' not in profile, (
            "Profile contains the broad /Users subpath allow — "
            "_ensure_executable_visible widened when it shouldn't "
            "have. This test relies on the helper interpreter "
            "living under cwd; check the symlink setup."
        )
        assert "file-read-metadata" in profile, (
            "Profile is missing the ancestor-traversal block "
            "(file-read-metadata literal allows). Without it Python "
            "will EPERM during realpath(sys.executable)."
        )

        result = subprocess.run(
            wrapped,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"sandbox-exec subprocess exited {result.returncode}.\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}\n\n"
            "If stderr matches r'python3: realpath: .*: Operation "
            "not permitted', the ancestor-traversal block in "
            "_build_profile regressed: realpath(sys.executable) "
            "needs file-read-metadata on each strict ancestor of "
            "cwd that isn't covered by a default RO subpath."
        )
        assert "HELLO" in result.stdout, (
            f"Subprocess ran but didn't print the expected marker. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_helper_boots_when_interpreter_lives_under_home_uv_layout(
    tmp_path: Path,
) -> None:
    """
    End-to-end regression: spawn a real ``sandbox-exec`` subprocess
    when ``argv[0]`` is a uv / pyenv / asdf-style Python interpreter
    located under ``$HOME``. Python startup must succeed and the
    subprocess must reach user code.

    Reproduces the user-reported failure: ``uv run`` places the
    managed Python under
    ``~/.local/share/uv/python/cpython-X.Y.Z-.../bin/python3.12``
    and creates a venv symlink in the project ``.venv/bin/python``.
    When the parent process IS that uv-managed Python, ``argv[0]``
    in the helper-spawn flow is ``sys.executable``, which resolves
    under ``/Users``. Pre-fix, ``_ensure_executable_visible`` raised
    OSError because widening to ``/Users`` would expose every other
    user's home; post-fix, it detects the canonical CPython install
    root and grants only that narrow subpath.

    Test fixture: a fake uv-style install layout under
    ``Path.home()`` (so the topmost ancestor IS ``/Users``,
    exercising the unsafe-widen guard) with the canonical shape
    (``<root>/bin/python*`` + ``<root>/lib/python<X>.<Y>``). The
    fake interpreter is a symlink to the test runner's real
    ``sys.executable`` so the spawn actually executes Python; the
    sandbox profile must grant both the fake install root (for the
    literal exec path) AND keep the resolved path covered by
    default RO subpaths (``/usr`` / ``/opt``).
    """
    import shutil as _shutil
    import subprocess
    import uuid

    if _shutil.which("sandbox-exec") is None:
        pytest.skip("sandbox-exec not on PATH")

    home = Path.home()
    fake_install_root = home / f".omnigent-uv-layout-regression-{uuid.uuid4().hex}"
    (fake_install_root / "bin").mkdir(parents=True)
    # ``lib/python<X>.<Y>`` is the marker the shape detector keys
    # off; the directory can be empty for the detection check.
    # Use the actual major.minor of the test runner so the layout
    # looks credible to any future audit reader.
    py_major_minor = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    (fake_install_root / "lib" / py_major_minor).mkdir(parents=True)

    try:
        # Symlink the test runner's REAL interpreter as
        # ``<install_root>/bin/python``. The literal exec path
        # (the symlink) is under the install root; the resolved
        # target (e.g. ``/opt/homebrew/.../python3.12`` on a
        # typical macOS dev box, or
        # ``~/.local/share/uv/python/...`` when the test runner is
        # itself uv-managed) is reached via symlink follow.
        real_python = Path(sys.executable).resolve(strict=True)
        fake_python = fake_install_root / "bin" / "python3"
        fake_python.symlink_to(real_python)

        cwd = tmp_path
        backend = _make_backend()
        policy = _make_policy(cwd.resolve(strict=False), allow_hidden=[".venv"])
        argv = [
            str(fake_python),
            "-c",
            "import sys; print('HELLO', sys.executable)",
        ]
        wrapped = backend.wrap_launcher_argv(argv, policy, cwd.resolve(strict=False))

        # Profile must contain a narrow subpath on the fake install
        # root — NOT a broad ``(subpath /Users)``. This is the
        # security-critical invariant: the auto-fallback widening
        # replaces (never coexists with) the topmost-ancestor grant.
        profile = Path(wrapped[2]).read_text()
        expected_narrow = (
            f'(allow file-read* (subpath "{fake_install_root.resolve(strict=False)}"))'
        )
        assert expected_narrow in profile, (
            "Profile is missing the narrow install-root subpath "
            f"({expected_narrow!r}); without it the auto-fallback "
            "regressed and the helper either raises OSError or "
            "the kernel EPERMs the exec. Generated profile:\n" + profile
        )
        assert '(allow file-read* (subpath "/Users"))' not in profile, (
            "Profile widened to (subpath /Users) — the narrow "
            "fallback degenerated into the broad widening the "
            "H1/H2/H3 hardening explicitly prohibits. This grants "
            "the sandboxed helper read access to every other "
            "user's home directory."
        )

        result = subprocess.run(
            wrapped,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"sandbox-exec subprocess exited {result.returncode}.\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}\n\n"
            "Common failure modes:\n"
            "- ``Operation not permitted`` during realpath: "
            "ancestor-traversal allows missing for the install "
            "root's parents (``/Users/<me>/...``).\n"
            "- ``Operation not permitted`` opening libpython: "
            "the install-root subpath didn't actually grant read "
            "access to ``lib/`` (check the profile)."
        )
        assert "HELLO" in result.stdout, (
            f"Helper ran but didn't print the expected marker. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    finally:
        _shutil.rmtree(fake_install_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Security hardening regression tests
#
# One test per hardening item from the security audit so a future
# edit that re-introduces the unsafe behavior fails loud rather than
# silently widening the sandbox. Each test docstring names the
# vulnerability id (H1, M1, …) so a triage reviewer can cross-reference
# with the audit report.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unsafe_root", sorted(_UNSAFE_WIDEN_ANCESTORS))
def test_h1_ensure_executable_visible_refuses_unsafe_topmost_ancestor(
    unsafe_root: str,
) -> None:
    """
    H1/H2/H3: ``_ensure_executable_visible`` MUST raise
    :class:`OSError` (not silently emit a broad ``(subpath ...)``
    rule) when the helper interpreter's topmost non-root ancestor
    is on the unsafe list (``/Users``, ``/private``, ``/var``, …).

    Pre-fix behaviour: silently appended the topmost ancestor as a
    read subpath, granting the sandboxed helper read access to every
    other user's home (or to the entire ``/var`` runtime state).
    A future edit that "fixes" a venv-outside-cwd test by re-adding
    silent widening would re-introduce the bypass; this test fails
    loud in that scenario.
    """
    # Construct a pseudo-venv path under the unsafe root.
    fake_interpreter = Path(unsafe_root) / "fake-pyenv" / "bin" / "python"
    argv = [str(fake_interpreter), "-c", "print('hi')"]
    # cwd elsewhere so the executable isn't covered by cwd allow.
    cwd = Path("/private/var/folders/regression-cwd")
    with pytest.raises(OSError) as exc:
        _ensure_executable_visible(argv, cwd)
    msg = str(exc.value)
    assert unsafe_root in msg, (
        f"OSError message should name the offending ancestor {unsafe_root!r}; got {msg!r}"
    )
    assert "sandbox-defeating widening" in msg, (
        "OSError message should explain WHY the widen was refused "
        "so the operator can pick a remediation."
    )


def test_h1_ensure_executable_visible_accepts_when_spec_grants_read_path(
    tmp_path: Path,
) -> None:
    """
    H1: when the operator explicitly grants ``read_paths`` covering
    the venv interpreter, ``_ensure_executable_visible`` returns ``[]``
    and does NOT raise — the spec-supplied path makes the widen
    unnecessary.

    This is the documented remediation #3 in the OSError message.
    Without this path, the only ways to run a venv-under-home setup
    would be to fall back to system Python or to move the venv into
    cwd; the explicit ``read_paths`` opt-in is the auditable middle
    ground.
    """
    fake_venv = Path("/Users/me/.pyenv/versions/3.12")
    fake_interpreter = fake_venv / "bin" / "python"
    argv = [str(fake_interpreter), "-c", "print('hi')"]
    extras = _ensure_executable_visible(
        argv,
        Path("/private/var/folders/other"),
        policy_read_roots=[fake_venv],
    )
    assert extras == [], (
        f"Spec-supplied read_paths covering the venv should make "
        f"_ensure_executable_visible a no-op; got extras={extras!r}"
    )


# ---------------------------------------------------------------------------
# Narrow Python-install-root fallback for HOME-anchored interpreters
# (uv / pyenv / asdf / rye / conda)
#
# When the helper interpreter's resolved path lives under an unsafe
# ancestor (``/Users`` on the common ``uv run`` layout), the sandbox
# refuses to widen to ``/Users`` (silently exposing every other user's
# home is a hard "no"), but instead detects the self-contained CPython
# install root and grants a narrow ``(subpath <root>)`` on that
# directory only. These tests pin that behaviour so a future edit
# can't accidentally re-introduce the broad ``/Users`` widening OR
# regress the unblocking of the ``uv run`` workflow.
# ---------------------------------------------------------------------------


def test_interpreter_install_root_detects_uv_python_layout(tmp_path: Path) -> None:
    """
    A directory with the canonical uv / python-build-standalone
    layout (``<root>/bin/python*`` + ``<root>/lib/python<X>.<Y>``)
    is recognised as a CPython install root by
    :func:`_interpreter_install_root`.

    The returned path is the *root* (the toolchain directory), not
    ``<root>/bin`` or ``<root>/bin/python*``. This is the directory
    the narrow SBPL ``(subpath ...)`` allow points at so the kernel
    can exec the binary AND dlopen the shared libs under ``lib/``.
    """
    install_root = tmp_path / "cpython-3.12.12-macos-aarch64-none"
    (install_root / "bin").mkdir(parents=True)
    (install_root / "lib" / "python3.12").mkdir(parents=True)
    exe = install_root / "bin" / "python3.12"
    exe.write_text("#!fake interpreter for shape detection\n")
    exe.chmod(0o755)
    detected = _interpreter_install_root(exe)
    assert detected is not None, (
        f"Expected canonical CPython install layout under {install_root!r} "
        "to be detected. Without this, ``uv run`` users with a Python "
        "under ``~/.local/share/uv/python/...`` can't start the helper."
    )
    assert detected == install_root.resolve(strict=False), (
        f"Detected install root {detected!r} should equal the resolved "
        f"toolchain directory {install_root.resolve(strict=False)!r}, "
        "NOT ``<root>/bin`` and NOT the interpreter file itself."
    )


def test_interpreter_install_root_detects_libpython_marker(tmp_path: Path) -> None:
    """
    The ``<root>/lib/libpython*.dylib`` marker is sufficient on its
    own — some slimmed-down CPython distributions ship the runtime
    dylib without a ``lib/python<X>.<Y>/`` stdlib directory (the
    stdlib is zipped into the binary or kept elsewhere). The shape
    check accepts either marker so it doesn't false-negative on
    those layouts.
    """
    install_root = tmp_path / "cpython-runtime-only"
    (install_root / "bin").mkdir(parents=True)
    lib_dir = install_root / "lib"
    lib_dir.mkdir()
    (lib_dir / "libpython3.12.dylib").write_bytes(b"\xfe\xed\xfa\xcf")  # mach-o magic
    exe = install_root / "bin" / "python3"
    exe.write_text("#!fake\n")
    exe.chmod(0o755)
    detected = _interpreter_install_root(exe)
    assert detected is not None, (
        "libpython*.dylib marker should match — slim CPython distributions "
        "without a lib/python*/ stdlib directory still need to be detected."
    )


def test_interpreter_install_root_rejects_arbitrary_home_directory(
    tmp_path: Path,
) -> None:
    """
    A HOME directory that happens to have ``bin/`` and ``lib/``
    siblings but no CPython marker (no ``lib/python*/`` stdlib
    directory, no ``lib/libpython*`` runtime) MUST NOT be reported
    as a Python install root.

    This is the security boundary: without the marker check, the
    function would happily report ``~/.local/`` itself as an install
    root the moment the user has any ``~/.local/lib/`` content,
    granting the sandbox read access to every cargo / npm / poetry
    artefact under ``~/.local`` and to whatever else the user has
    cached there.
    """
    fake_root = tmp_path / "not-a-python-install"
    (fake_root / "bin").mkdir(parents=True)
    (fake_root / "lib").mkdir()
    # Sibling content that's plausible in ``~/.local/lib`` but isn't
    # a CPython marker (no ``python*`` dir, no ``libpython*`` file).
    (fake_root / "lib" / "node_modules").mkdir()
    (fake_root / "lib" / "libfoo.dylib").write_bytes(b"\x00")
    exe = fake_root / "bin" / "python"  # name doesn't matter — shape does
    exe.write_text("#!fake\n")
    exe.chmod(0o755)
    detected = _interpreter_install_root(exe)
    assert detected is None, (
        f"Got detected={detected!r}; expected None for a directory "
        "with bin/+lib/ but no CPython marker. Without this rejection "
        "the auto-widen path would silently grant read access to "
        "arbitrary HOME subtrees that happen to have a bin/lib shape."
    )


def test_interpreter_install_root_rejects_missing_lib_directory(
    tmp_path: Path,
) -> None:
    """
    A binary whose parent is named ``bin`` but whose grand-parent
    lacks a ``lib/`` sibling cannot be a self-contained CPython
    install. :func:`_interpreter_install_root` returns ``None`` so
    the caller falls through to the OSError path.
    """
    fake_root = tmp_path / "only-bin"
    (fake_root / "bin").mkdir(parents=True)
    exe = fake_root / "bin" / "python"
    exe.write_text("#!fake\n")
    exe.chmod(0o755)
    detected = _interpreter_install_root(exe)
    assert detected is None, (
        f"Got detected={detected!r}; expected None when <root>/lib "
        "is absent. The CPython runtime + stdlib live under lib/; "
        "an install without lib/ can't be a self-contained CPython."
    )


def test_interpreter_install_root_rejects_when_parent_is_not_bin(
    tmp_path: Path,
) -> None:
    """
    A binary whose parent directory isn't named ``bin`` doesn't
    match the canonical CPython entry-point layout, even when the
    grand-parent has a ``lib/python*/`` shape. This guards against
    incidental matches on unusual interpreter wrappers and on
    binaries planted at deeper layouts.
    """
    install_root = tmp_path / "cpython-3.12"
    (install_root / "Scripts").mkdir(parents=True)  # not "bin"
    (install_root / "lib" / "python3.12").mkdir(parents=True)
    exe = install_root / "Scripts" / "python3.12"
    exe.write_text("#!fake\n")
    exe.chmod(0o755)
    detected = _interpreter_install_root(exe)
    assert detected is None, (
        "Parent directory not named 'bin' must not match — only the "
        "canonical CPython ``<root>/bin/python*`` layout qualifies."
    )


def test_ensure_executable_visible_falls_back_to_install_root_for_uv_layout(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    The canonical ``uv run`` reproduction: the parent's
    ``sys.executable`` resolves to
    ``<unsafe_ancestor>/.../cpython-X.Y.Z-.../bin/python3.12``.
    :func:`_ensure_executable_visible` MUST detect the install root
    and return a narrow widening on it instead of raising OSError.

    The test stages the install layout under ``tmp_path`` (which on
    macOS lives under ``/private/var/folders`` — covered by default
    RO subpaths — but we explicitly thread an unsafe-ancestor case
    via the policy_read_roots argument so the test is
    platform-agnostic). A WARNING must be emitted naming both the
    refused ancestor and the granted install root so the auto-widen
    is auditable.
    """
    # Build a uv-style install layout that lives under an unsafe
    # ancestor by constructing the path strings; the function
    # inspects paths, not filesystem inodes, for the install-root
    # detection EXCEPT for the lib/ marker check. Stage the
    # filesystem under tmp_path and point the argv at the real path.
    install_root = tmp_path / "fake-home" / ".local" / "share" / "uv" / "python" / "cpython-3.12"
    (install_root / "bin").mkdir(parents=True)
    (install_root / "lib" / "python3.12").mkdir(parents=True)
    exe = install_root / "bin" / "python3.12"
    exe.write_text("#!fake\n")
    exe.chmod(0o755)

    # Construct an argv where argv[0] is a SYMLINK from outside
    # ``tmp_path`` (so the literal exec path is covered by cwd, but
    # the resolved path is the real install under tmp_path). The
    # test caller doesn't have an unsafe-ancestor symlink to play
    # with here, so we drive the fallback via the resolved-path
    # branch by passing the install path directly as argv[0] and
    # using a cwd elsewhere.
    argv = [str(exe), "-c", "print('hi')"]

    # Use cwd that doesn't cover the install root so we exercise
    # the topmost-ancestor branch.
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()

    # Patch _UNSAFE_WIDEN_ANCESTORS to include the topmost-ancestor
    # of ``install_root`` so we trigger the narrow-fallback branch
    # without depending on the test host's layout. ``tmp_path`` on
    # macOS is ``/private/var/folders/...``; on Linux it's
    # ``/tmp/...``. Either way the topmost is a real path on disk.
    topmost_str = "/" + str(install_root).lstrip("/").split("/", 1)[0]
    with patch(
        "omnigent.inner.seatbelt_sandbox._UNSAFE_WIDEN_ANCESTORS",
        frozenset({topmost_str}),
    ):
        with caplog.at_level("WARNING", logger="omnigent.inner.seatbelt_sandbox"):
            extras = _ensure_executable_visible(argv, cwd)

    assert extras, (
        f"Expected a narrow install-root widen; got empty extras. "
        f"The uv-style layout {install_root!r} should trigger the "
        "_interpreter_install_root fallback before the OSError raise."
    )
    expected_root = install_root.resolve(strict=False)
    assert expected_root in extras, (
        f"Extras={extras!r} should contain the detected install "
        f"root {expected_root!r}. The narrow widening grants read "
        "access to the toolchain only, not to ${{HOME}} or above."
    )
    assert all(str(p) != topmost_str for p in extras), (
        f"Extras={extras!r} contains the unsafe topmost ancestor "
        f"{topmost_str!r}. The narrow fallback must REPLACE the "
        "topmost grant, never coexist with it — otherwise the "
        "broad allow re-introduces the bypass the H1/H2/H3 "
        "hardening exists to prevent."
    )
    assert any(
        "narrow read-only" in record.message and "install root" in record.message
        for record in caplog.records
    ), (
        "Expected an audit-grade WARNING naming the narrow install "
        "root; got log records: "
        f"{[(r.levelname, r.message[:80]) for r in caplog.records]!r}. "
        "Without the warning operators can't tell from logs that "
        "the sandbox auto-widened to a HOME-anchored install."
    )


def test_ensure_executable_visible_still_raises_for_non_python_home_layouts(
    tmp_path: Path,
) -> None:
    """
    Negative complement of the uv-fallback test: when the helper
    interpreter is HOME-anchored but the directory layout does NOT
    match the canonical CPython install shape (no ``lib/python*/``,
    no ``lib/libpython*``), :func:`_ensure_executable_visible` MUST
    fall through to the OSError raise. Otherwise the narrow
    fallback would degrade into a broad-pattern widening that
    accepts arbitrary HOME paths.

    The OSError message must mention that auto-detection was tried
    so the operator can debug why their layout didn't match.
    """
    fake_root = tmp_path / "fake-home" / "custom-runtime"
    (fake_root / "bin").mkdir(parents=True)
    (fake_root / "lib").mkdir()
    # Plausible non-Python content under ``lib/``.
    (fake_root / "lib" / "libruby.dylib").write_bytes(b"\x00")
    exe = fake_root / "bin" / "python"
    exe.write_text("#!fake\n")
    exe.chmod(0o755)
    argv = [str(exe), "-c", "print('hi')"]
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()

    topmost_str = "/" + str(fake_root).lstrip("/").split("/", 1)[0]
    with patch(
        "omnigent.inner.seatbelt_sandbox._UNSAFE_WIDEN_ANCESTORS",
        frozenset({topmost_str}),
    ):
        with pytest.raises(OSError) as exc:
            _ensure_executable_visible(argv, cwd)

    msg = str(exc.value)
    assert "Auto-detection of a narrow Python install root" in msg, (
        f"OSError should explain that the install-root fallback was "
        f"attempted; got message: {msg!r}. Without this hint, an "
        "operator hitting a near-miss layout (e.g. they forgot to "
        "install the stdlib) can't tell why the auto-widen didn't "
        "save them."
    )


def test_h4_resolve_root_does_not_expand_env_vars_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """
    H4: ``_resolve_root`` MUST NOT expand ``$VAR`` against the
    parent environment. Pre-fix, an attacker who could shape the
    parent's env (parent shell, MCP server, supervisor agent, or
    unaudited spec templating) could rewrite
    ``read_paths: ['$LOG_DIR/audit']`` into ``read_paths: ['/']``
    and silently widen the sandbox to the whole filesystem.

    The literal string ``$LOG_DIR`` must survive resolution
    unchanged (treated as a path component) and a warning must be
    emitted so over-broad expansions stand out in logs.
    """
    monkeypatch.setenv("LOG_DIR", "/")
    with caplog.at_level("WARNING", logger="omnigent.inner.seatbelt_sandbox"):
        resolved = _resolve_root(tmp_path, "$LOG_DIR/audit")
    # The literal ``$LOG_DIR`` survives — resolved path ends with
    # the unexpanded segment, NOT with ``/audit`` rooted at ``/``.
    assert "$LOG_DIR" in str(resolved), (
        f"Got {resolved!r}; expected the literal ``$LOG_DIR`` to "
        "survive resolution. If the resolver expanded the env var, "
        "the H4 hardening regressed and the sandbox is widenable "
        "by a same-host attacker who can shape the parent's env."
    )
    assert any("no longer expanded" in record.message for record in caplog.records), (
        "Resolver should warn when a spec path contains ``$`` so "
        "broad-by-mistake specs are visible in operator logs."
    )


def test_l5_resolve_root_warns_on_broad_paths(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """
    L5: ``_resolve_root`` MUST emit a warning when the resolved path
    matches one of the documented over-broad roots (``/``, ``/Users``,
    ``/var``, …). Not blocked — some legitimate agents need a wide
    grant — but the warning makes the choice auditable in logs.
    """
    with caplog.at_level("WARNING", logger="omnigent.inner.seatbelt_sandbox"):
        _resolve_root(tmp_path, "/")
    assert any("near-unrestricted" in record.message for record in caplog.records), (
        "Resolver should warn when a spec path resolves to a "
        "documented broad root. Without this, an operator who "
        "ships ``read_paths: ['/']`` in a misconfigured spec "
        "wouldn't see the choice in logs at all."
    )


def test_m1_m2_profile_excludes_mach_priv_host_port_and_iokit_open(
    tmp_path: Path,
) -> None:
    """
    M1/M2: the SBPL profile MUST NOT include
    ``(allow mach-priv-host-port)`` or ``(allow iokit-open)``.

    M1 (mach-priv-host-port): grants kernel-task IPC access. Not
    used by the Python helper for any legitimate purpose; common
    lever for sandbox-escape exploits.

    M2 (iokit-open): grants access to every IOKit driver including
    camera, microphone, GPU. Helper has no use for any of them.

    Reference seatbelt profiles in the wild often grant these
    broadly. A future "let's match Apple's reference profile" edit
    would silently re-introduce them; this test fails loud.
    """
    policy = _make_policy(tmp_path)
    profile = _build_profile(policy, tmp_path)
    # Match the (allow ...) form rather than the bare token because
    # the profile body intentionally MENTIONS the removed forms in
    # comments documenting the hardening choice. We only care that
    # they aren't actually granted.
    assert "(allow mach-priv-host-port" not in profile, (
        "Profile contains mach-priv-host-port allow — M1 regression. "
        "Grants kernel-task IPC, common sandbox-escape lever."
    )
    assert "(allow iokit-open" not in profile, (
        "Profile contains iokit-open allow — M2 regression. Grants "
        "access to every IOKit driver including camera / mic / GPU."
    )


def test_m4_profile_narrows_dev_write_to_specific_literals(tmp_path: Path) -> None:
    """
    M4: the SBPL profile MUST narrow ``/dev`` write access to a
    small set of safe literals (``/dev/null``, ``/dev/tty``,
    ``/dev/dtracehelper``) rather than granting ``(allow file-write*
    (subpath "/dev"))`` which would allow writes through arbitrary
    character / block devices (e.g. ``/dev/disk*`` for direct disk
    access, ``/dev/console`` for system log spoofing).

    Reads on ``/dev`` stay broad — needed for ``/dev/urandom``,
    ``/dev/fd/N`` etc. — but writes must be per-literal.
    """
    policy = _make_policy(tmp_path)
    profile = _build_profile(policy, tmp_path)
    assert '(allow file-write* (subpath "/dev"))' not in profile, (
        'Profile contains broad (subpath "/dev") write allow — '
        "M4 regression. Lets the helper write through arbitrary "
        "devices including disk character / block devices."
    )
    # Required narrow allows must be present.
    assert '(allow file-write* (literal "/dev/null"))' in profile
    assert '(allow file-write* (literal "/dev/tty"))' in profile


def test_l1_quote_rejects_control_characters() -> None:
    """
    L1: ``_quote`` MUST raise :class:`ValueError` on input
    containing ASCII control characters (``\\x00``-``\\x1f``,
    ``\\x7f``). The backslash-escape pass handles ``\\`` and ``"``
    but does NOT escape control bytes; a malicious path carrying
    ``\\x0a`` would otherwise let an attacker who can shape paths
    inject extra SBPL forms after a newline.
    """
    with pytest.raises(ValueError):
        _quote("/tmp/foo\nbar")
    with pytest.raises(ValueError):
        _quote("/tmp/foo\x00bar")
    with pytest.raises(ValueError):
        _quote("/tmp/foo\x7fbar")
    # Sanity-check: normal paths still quote cleanly.
    assert _quote("/tmp/foo") == '"/tmp/foo"'


def test_m5_wrap_launcher_argv_writes_profile_to_mode_0600_tempfile(
    tmp_path: Path,
) -> None:
    """
    M5: ``wrap_launcher_argv`` MUST pass the profile via a
    mode-0600 tempfile (``-f <path>``) rather than inline
    (``-p <text>``). Inline profiles appear in ``ps aux`` output
    for every same-host user; the file form leaks only the path,
    and the file itself is unreadable by other users (mode 0600).
    """
    import os as _os
    import stat as _stat

    if sys.platform != "darwin":
        pytest.skip("seatbelt wrap requires macOS host")
    backend = _make_backend()
    helper = _safe_helper_argv(tmp_path)
    cwd = Path(helper[0]).parent.parent.parent  # tmp_path
    policy = _make_policy(cwd, allow_hidden=[".venv"])
    wrapped = backend.wrap_launcher_argv(helper, policy, cwd, chdir=None)
    assert wrapped[1] == "-f", (
        f"Expected ``-f <path>`` form for profile; got {wrapped[1]!r}. "
        "If this is ``-p``, the M5 hardening regressed and the full "
        "SBPL profile is visible to every same-host user via ps."
    )
    profile_path = Path(wrapped[2])
    assert profile_path.exists(), "Profile tempfile should exist on disk"
    mode = _stat.S_IMODE(_os.stat(profile_path).st_mode)
    assert mode == 0o600, (
        f"Profile tempfile mode is 0o{mode:o}; expected 0o600. "
        "Other modes leak the profile (and the dotfile-mask paths "
        "it embeds) to same-host users."
    )


def test_m6_wrap_launcher_argv_uses_absolute_sandbox_exec_path(
    tmp_path: Path,
) -> None:
    """
    M6: ``wrap_launcher_argv`` MUST invoke ``sandbox-exec`` by its
    absolute path captured at module-import time, not by name. An
    attacker who can mutate ``$PATH`` between the resolver's
    ``shutil.which`` check and the spawn could otherwise substitute
    a malicious ``sandbox-exec`` earlier in the search path.
    """
    if sys.platform != "darwin":
        pytest.skip("seatbelt wrap requires macOS host")
    backend = _make_backend()
    helper = _safe_helper_argv(tmp_path)
    cwd = Path(helper[0]).parent.parent.parent
    policy = _make_policy(cwd, allow_hidden=[".venv"])
    wrapped = backend.wrap_launcher_argv(helper, policy, cwd, chdir=None)
    assert wrapped[0].startswith("/"), (
        f"wrap_launcher_argv used a non-absolute first element "
        f"{wrapped[0]!r}; M6 regression. ``subprocess.Popen`` will "
        "do a ``$PATH`` lookup and an attacker who can shape "
        "``$PATH`` can swap in a malicious sandbox-exec."
    )
    assert wrapped[0] == _SANDBOX_EXEC_PATH


def test_s1_default_read_subpaths_omits_broad_private_var_folders(
    tmp_path: Path,
) -> None:
    """
    S1: ``/private/var/folders`` MUST NOT appear as a broad
    ``(allow file-read* (subpath ...))`` in the profile. The path
    is per-user, not per-helper, and granting it lets one
    concurrent helper read every OTHER same-user helper's scratch
    tmpdir (mkdtemp 0700 doesn't protect against same-UID
    processes — only the sandbox does). Cross-helper scratch
    isolation breaks if this constant returns.

    The helper's own scratch is still granted via its specific
    subpath; this test only pins the absence of the broad allow.
    """
    assert "/private/var/folders" not in _DEFAULT_READ_SUBPATHS, (
        "/private/var/folders is back in _DEFAULT_READ_SUBPATHS — "
        "every concurrent same-user helper can now read every "
        "other helper's scratch tmpdir. S1 regression."
    )
    policy = _make_policy(tmp_path)
    profile = _build_profile(policy, tmp_path.resolve(strict=False))
    forbidden = '(allow file-read* (subpath "/private/var/folders"))'
    assert forbidden not in profile, (
        "Profile contains the broad /private/var/folders subpath "
        "allow. This grants cross-helper scratch reads to every "
        "same-user concurrent helper. S1 regression."
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="dyld cache is macOS-only")
def test_s1_per_user_dyld_cache_subpath_is_under_per_user_folder() -> None:
    """
    S1: when :func:`_per_user_dyld_cache_subpath` returns a path,
    it MUST live under the per-user folder
    (``Path(tempfile.gettempdir()).parent``) and specifically under
    its ``C/com.apple.dyld`` subdir. A widening of this resolution
    to a sibling that contains other users' or other helpers'
    data would re-introduce the cross-helper leak.
    """
    import tempfile as _tempfile

    cache = _per_user_dyld_cache_subpath()
    if cache is None:
        pytest.skip("dyld cache not present on this host layout")
    per_user_folder = Path(_tempfile.gettempdir()).resolve(strict=False).parent
    expected = per_user_folder / "C" / "com.apple.dyld"
    assert cache == expected, (
        f"Got dyld cache path {cache!r}; expected {expected!r}. "
        "A different resolution would either miss the real cache "
        "(dyld slow-path on every helper boot) or widen to a "
        "sibling that carries per-user secrets."
    )


def test_m7_resolve_warns_when_cwd_allow_hidden_contains_sensitive_dotfile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    M7: the resolver MUST emit a warning when ``cwd_allow_hidden``
    includes a basename matching the documented sensitive set
    (``.aws``, ``.ssh``, ``.netrc``, …). Not blocked — some agents
    legitimately need these — but the warning makes the choice
    auditable so an unintended grant doesn't go unnoticed.
    """
    if sys.platform != "darwin":
        pytest.skip("seatbelt resolver requires macOS host")
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(
            type="darwin_seatbelt",
            cwd_allow_hidden=[".aws", ".ssh", ".venv"],
        ),
    )
    with caplog.at_level("WARNING", logger="omnigent.inner.seatbelt_sandbox"):
        backend.resolve(spec, Path.cwd())
    msgs = " ".join(record.message for record in caplog.records)
    assert ".aws" in msgs and ".ssh" in msgs, (
        "Resolver should warn about each sensitive dotfile name "
        "in cwd_allow_hidden; M7 regression."
    )
    assert ".venv" not in msgs, (
        ".venv is the default opt-in and is NOT on the sensitive "
        "list — warning on it would flood operator logs."
    )


# ---------------------------------------------------------------------------
# S5: HOME-anchored sensitive subpath denials + read_paths dotfile masking.
#
# Threat: a broad ``read_paths`` grant (typically ``["~/"]`` for an
# agent that needs project siblings) used to silently expose the
# operator's credential stores — ``~/.aws/credentials``,
# ``~/.ssh/id_*``, ``~/Library/Cookies``, etc. — because:
#
# 1. The dotfile masker was cwd-only, so it never walked ``read_paths``.
# 2. ``~/Library`` isn't dotfile-shaped, so the dotfile masker
#    couldn't catch it even if it had walked HOME.
#
# Fix:
#
# 1. The dotfile masker now walks every ``read_paths`` root in
#    addition to ``cwd``, honouring the same ``cwd_allow_hidden``
#    allowlist. So ``read_paths: ["~/"]`` masks ``~/.aws`` etc.
# 2. macOS-specific: ``$HOME/Library`` is denied by default unless
#    the spec explicitly names ``~/Library`` (or a path under it)
#    in ``read_paths`` — naming an ancestor like ``~/`` does NOT
#    count as opt-in.
# ---------------------------------------------------------------------------


def test_s5_home_library_denied_by_default_when_home_in_read_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    With ``read_paths: ["~/"]`` granted, the profile MUST still
    contain a ``(deny ... (subpath "$HOME/Library"))`` rule. SBPL
    deny-wins-over-allow means this deny overrides the broad HOME
    read grant exactly for the Library subtree — the operator's
    browser cookies / Slack tokens / app keychains stay invisible
    to the helper even when HOME is otherwise readable.

    Without this rule, ``read_paths: ["~/"]`` would silently expose
    every Chrome cookie and Mail message on the host (none of which
    are dotfile-shaped so the dotfile masker can't catch them).
    """
    # Anchor HOME at a writable test path so the profile builder's
    # ``~`` expansion is deterministic on whatever box the test
    # runs on (real HOME has a real ~/Library that may or may not
    # exist; tmp_path is a controlled fixture).
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    cwd = tmp_path / "work"
    cwd.mkdir()

    policy = _make_policy(cwd, read_roots=[fake_home.resolve(strict=False)])
    profile = _build_profile(policy, cwd.resolve(strict=False))

    expected_deny = (
        f'(deny file-read* file-write* (subpath "{fake_home.resolve(strict=False) / "Library"}"))'
    )
    assert expected_deny in profile, (
        "Expected $HOME/Library to be denied by default even when "
        "read_paths grants HOME. Without this deny, a broad "
        "read_paths grant silently exposes every browser cookie / "
        "Slack token / app keychain on the host."
    )


def test_s5_home_library_allowed_when_explicitly_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    When the spec explicitly names ``$HOME/Library`` in
    ``read_paths``, the default-deny is suppressed. This is the
    opt-in path for operators who legitimately need
    ``~/Library/Logs`` debugging access or similar. Naming the
    candidate itself (or a path *under* it) clears the deny;
    naming an ancestor like ``~/`` does NOT.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    library = fake_home / "Library"
    library.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    cwd = tmp_path / "work"
    cwd.mkdir()

    policy = _make_policy(
        cwd,
        read_roots=[
            fake_home.resolve(strict=False),
            library.resolve(strict=False),
        ],
    )
    profile = _build_profile(policy, cwd.resolve(strict=False))

    unwanted_deny = f'(deny file-read* file-write* (subpath "{library.resolve(strict=False)}"))'
    assert unwanted_deny not in profile, (
        "Explicit read_paths entry for $HOME/Library should "
        "suppress the default deny — the operator opted in."
    )


def test_s5_home_library_deny_suppressed_by_under_path_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Naming a narrower subtree (``~/Library/Logs``) should also
    suppress the default deny — operators don't have to grant the
    whole ``~/Library`` tree to opt into a specific subdir. The
    suppression rule is "at-or-under the candidate".
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    library_logs = fake_home / "Library" / "Logs"
    library_logs.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    cwd = tmp_path / "work"
    cwd.mkdir()

    policy = _make_policy(cwd, read_roots=[library_logs.resolve(strict=False)])
    profile = _build_profile(policy, cwd.resolve(strict=False))

    library_deny_subpath = (
        f'(deny file-read* file-write* (subpath "{fake_home.resolve(strict=False) / "Library"}"))'
    )
    assert library_deny_subpath not in profile, (
        "Granting a path under $HOME/Library should clear the "
        "default deny on $HOME/Library (operator opted in via the "
        "narrower grant)."
    )


def test_s5_home_library_deny_not_suppressed_by_ancestor_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The opt-in must be "at-or-under" the candidate, NOT "ancestor
    of". Granting ``~/`` (an ANCESTOR of ``~/Library``) does NOT
    count as opting into ``~/Library`` — otherwise the whole point
    of the default-deny would be defeated by the most common spec
    shape.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    cwd = tmp_path / "work"
    cwd.mkdir()

    policy = _make_policy(cwd, read_roots=[fake_home.resolve(strict=False)])
    profile = _build_profile(policy, cwd.resolve(strict=False))

    expected_deny = (
        f'(deny file-read* file-write* (subpath "{fake_home.resolve(strict=False) / "Library"}"))'
    )
    assert expected_deny in profile, (
        "Granting an ancestor of $HOME/Library (e.g. ~/) must NOT "
        "suppress the default deny — that would defeat the protection."
    )


def test_s5_read_paths_dotfile_masking_blocks_dot_aws_under_home_grant(
    tmp_path: Path,
) -> None:
    """
    With ``read_paths: [<dir-with-dotfiles>]``, the per-path dotfile
    masker MUST also walk that directory and emit deny rules for the
    dotfiles found there — not just for cwd. The pre-fix behaviour
    was cwd-only, so a broad ``read_paths`` grant would silently
    expose ``.aws``, ``.ssh``, ``.netrc`` etc. living under the
    granted path.
    """
    # Fake-home shape: dotfiles + a regular file. We don't touch
    # ``$HOME`` here — this test is purely about the dotfile masker
    # walking read_paths roots, independent of the macOS Library
    # default-deny.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".aws").mkdir()
    (fake_home / ".aws" / "credentials").write_text("[default]\nkey=secret")
    (fake_home / ".ssh").mkdir()
    (fake_home / ".ssh" / "id_ed25519").write_text("-----BEGIN")
    (fake_home / ".env").write_text("SECRET=1")
    (fake_home / "code").mkdir()  # non-dotfile — must NOT be masked
    cwd = tmp_path / "work"
    cwd.mkdir()

    policy = _make_policy(
        cwd,
        read_roots=[fake_home.resolve(strict=False)],
        allow_hidden=[".venv"],  # the default
    )
    profile = _build_profile(policy, cwd.resolve(strict=False))

    aws_deny = (
        f'(deny file-read* file-write* (subpath "{fake_home.resolve(strict=False) / ".aws"}"))'
    )
    ssh_deny = (
        f'(deny file-read* file-write* (subpath "{fake_home.resolve(strict=False) / ".ssh"}"))'
    )
    env_deny = (
        f'(deny file-read* file-write* (literal "{fake_home.resolve(strict=False) / ".env"}"))'
    )
    code_deny_subpath = (
        f'(deny file-read* file-write* (subpath "{fake_home.resolve(strict=False) / "code"}"))'
    )

    assert aws_deny in profile, (
        "Dotfile masker did not walk read_paths root — .aws/ is "
        "still exposed. Regression of the S5 fix that extended the "
        "walker beyond cwd."
    )
    assert ssh_deny in profile, (
        ".ssh/ under a read_paths root is still exposed; same regression as the .aws case."
    )
    assert env_deny in profile, (
        ".env file under a read_paths root is still exposed; the "
        "walker should emit a (literal ...) deny for regular files."
    )
    assert code_deny_subpath not in profile, (
        "Non-dotfile entries under read_paths must NOT be masked — "
        "the whole point of granting the read_paths root is to "
        "expose the project files."
    )


def test_s5_read_paths_dotfile_masking_honors_cwd_allow_hidden(
    tmp_path: Path,
) -> None:
    """
    Operators opt into a specific dotfile-shaped path by naming its
    basename in ``cwd_allow_hidden``. That allowlist MUST be honored
    for read_paths roots too — otherwise the only way to grant
    ``.aws`` through would be to drop the whole walker, which would
    re-open the bigger hole.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".aws").mkdir()
    (fake_home / ".aws" / "credentials").write_text("[default]")
    (fake_home / ".ssh").mkdir()  # NOT in the allowlist
    cwd = tmp_path / "work"
    cwd.mkdir()

    policy = _make_policy(
        cwd,
        read_roots=[fake_home.resolve(strict=False)],
        allow_hidden=[".aws"],
    )
    profile = _build_profile(policy, cwd.resolve(strict=False))

    aws_deny_literal = (
        f'(deny file-read* file-write* (literal "{fake_home.resolve(strict=False) / ".aws"}"))'
    )
    aws_deny_subpath = (
        f'(deny file-read* file-write* (subpath "{fake_home.resolve(strict=False) / ".aws"}"))'
    )
    ssh_deny = (
        f'(deny file-read* file-write* (subpath "{fake_home.resolve(strict=False) / ".ssh"}"))'
    )
    assert aws_deny_literal not in profile and aws_deny_subpath not in profile, (
        ".aws is in cwd_allow_hidden but a deny rule still landed "
        "for it under the read_paths root — the allowlist filter "
        "is not being applied to read_paths."
    )
    assert ssh_deny in profile, (
        ".ssh is NOT in the allowlist; it must still be masked under read_paths roots."
    )


def test_s5_read_paths_dedup_skips_paths_under_cwd(tmp_path: Path) -> None:
    """
    A ``read_paths`` entry that lives at-or-under ``cwd`` is fully
    covered by the cwd dotfile scan — the read_paths walker must
    skip it to avoid emitting the same per-path deny twice (which
    is harmless to SBPL but bloats the profile and risks tripping
    :data:`_MAX_PROFILE_BYTES` on big workspaces).
    """
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / ".env").write_text("SECRET=1")
    sub = cwd / "sub"
    sub.mkdir()

    policy = _make_policy(
        cwd,
        read_roots=[
            cwd.resolve(strict=False),
            sub.resolve(strict=False),
        ],
        allow_hidden=[".venv"],
    )
    profile = _build_profile(policy, cwd.resolve(strict=False))

    env_deny = f'(deny file-read* file-write* (literal "{cwd.resolve(strict=False) / ".env"}"))'
    assert profile.count(env_deny) == 1, (
        ".env masked more than once — the dedup that skips "
        "read_paths roots at-or-under cwd regressed."
    )


# ---------------------------------------------------------------------------
# Exec-chain symlink hops + launcher target visibility (bwrap parity)
# ---------------------------------------------------------------------------


def _uv_style_layout(base: Path) -> tuple[Path, Path, Path]:
    """
    Stage uv's tool-venv → versionless-hop → real-install layout.

    Mirrors what ``uv tool install omnigent`` produces: the tool
    venv's ``bin/python`` symlinks to a path that traverses the
    version-floating ``cpython-3.12`` directory symlink before
    landing on the real ``cpython-3.12.13`` install. Both roots get
    the venv/install shape (``bin/`` + ``lib/python3.12``) so the
    narrow install-root fallback matches them.

    :param base: Directory to stage the fake ``$HOME`` layout under.
    :returns: ``(tool_exe, versionless_dir, real_install_root)``.
    """
    real_root = base / ".local" / "share" / "uv" / "python" / "cpython-3.12.13-macos"
    (real_root / "bin").mkdir(parents=True)
    (real_root / "lib" / "python3.12").mkdir(parents=True)
    real_exe = real_root / "bin" / "python3.12"
    real_exe.write_text("#!fake\n")
    real_exe.chmod(0o755)
    versionless = base / ".local" / "share" / "uv" / "python" / "cpython-3.12-macos"
    versionless.symlink_to("cpython-3.12.13-macos")
    tool_root = base / ".local" / "share" / "uv" / "tools" / "omnigent"
    (tool_root / "bin").mkdir(parents=True)
    (tool_root / "lib" / "python3.12").mkdir(parents=True)
    tool_exe = tool_root / "bin" / "python"
    tool_exe.symlink_to(versionless / "bin" / "python3.12")
    return tool_exe, versionless, real_root


def test_wrap_launcher_argv_grants_uv_versionless_hop_literal(tmp_path: Path) -> None:
    """
    The uv boot regression: the helper interpreter is reached through
    uv's version-floating directory symlink
    (``cpython-3.12 -> cpython-3.12.13``). The profile grants a
    subpath on the RESOLVED install root, but execve reads the
    versionless symlink at its LITERAL path — without a literal
    grant for the hop, ``sandbox-exec``'s execvp of the helper dies
    with EPERM and every jailed helper spawn fails at boot.
    """
    tool_exe, versionless, real_root = _uv_style_layout(tmp_path / "fake-home")
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    backend = _make_backend()
    policy = _make_policy(cwd, allow_hidden=[".venv"])
    argv = backend.wrap_launcher_argv(
        [str(tool_exe), "-m", "omnigent.inner.os_env", "helper", "X"], policy, cwd
    )
    profile = Path(argv[2]).read_text()

    hop_literal = f"(allow file-read* (literal {_quote(str(versionless))}))"
    assert hop_literal in profile, (
        f"Missing literal read grant for the versionless symlink hop "
        f"{str(versionless)!r}. Subpath rules match only canonical "
        "paths, so without this literal the kernel denies reading the "
        "symlink during execve resolution and the helper interpreter "
        "never boots (execvp EPERM)."
    )
    resolved_subpath = (
        f"(allow file-read* (subpath {_quote(str(real_root.resolve(strict=False)))}))"
    )
    assert resolved_subpath in profile, (
        "The resolved install root must keep its narrow subpath grant "
        "— the hop literal complements it, never replaces it."
    )


def test_wrap_launcher_argv_target_grants_claude_cli_install(tmp_path: Path) -> None:
    """
    The launcher target (e.g. the claude CLI wrapped by
    ``prepare_claude_cli_path``) must be readable inside the sandbox:
    the standalone installer puts a symlink at ``~/.local/bin/claude``
    pointing into ``~/.local/share/claude/versions/<v>/``. The wrap
    must grant a literal read on the symlink and a subpath on the
    version directory — and nothing wider — instead of discarding
    ``target`` (which crashed the whole native-tool wrap with
    ``PermissionError`` at connect time).
    """
    fake_home = tmp_path / "fake-home"
    version_dir = fake_home / ".local" / "share" / "claude" / "versions" / "1.0.61"
    version_dir.mkdir(parents=True)
    cli_real = version_dir / "claude"
    cli_real.write_bytes(b"\x00fakebun")
    cli_real.chmod(0o755)
    bin_dir = fake_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    cli_link = bin_dir / "claude"
    cli_link.symlink_to(cli_real)
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    backend = _make_backend()
    policy = _make_policy(cwd, allow_hidden=[".venv"])
    argv = backend.wrap_launcher_argv(_safe_helper_argv(cwd), policy, cwd, target=str(cli_link))
    profile = Path(argv[2]).read_text()

    assert f"(allow file-read* (literal {_quote(str(cli_link))}))" in profile, (
        "The target symlink needs a literal read grant — the kernel "
        "reads it at its literal path when the launcher execs the CLI."
    )
    assert f"(allow file-read* (subpath {_quote(str(version_dir))}))" in profile, (
        "The resolved CLI's own directory needs a subpath grant so the "
        "in-sandbox exec can read the binary."
    )
    dotlocal = fake_home / ".local"
    assert f"(allow file-read* (subpath {_quote(str(dotlocal))}))" not in profile, (
        "The grant must stay scoped to the CLI install — widening to "
        "~/.local would expose sibling tool state."
    )
    assert f"(allow file-read-metadata (literal {_quote(str(bin_dir))}))" in profile, (
        "The literal grant's ancestors need stat-only traversal allows "
        "or realpath() walks to the CLI EPERM under deny-default."
    )


def test_wrap_launcher_argv_target_under_cwd_changes_nothing(tmp_path: Path) -> None:
    """
    A target already covered by the cwd subpath must not change the
    profile — no redundant grants, profile contents byte-identical to
    the no-target wrap (mirrors the bwrap no-op-target behaviour).
    """
    backend = _make_backend()
    policy = _make_policy(tmp_path, allow_hidden=[".venv"])
    tool = tmp_path / "tools" / "cli"
    tool.parent.mkdir()
    tool.write_text("#!fake\n")
    tool.chmod(0o755)
    helper_argv = _safe_helper_argv(tmp_path)

    argv_no_target = backend.wrap_launcher_argv(helper_argv, policy, tmp_path)
    argv_with_target = backend.wrap_launcher_argv(helper_argv, policy, tmp_path, target=str(tool))
    assert Path(argv_no_target[2]).read_text() == Path(argv_with_target[2]).read_text(), (
        "A cwd-covered target must be a no-op for the profile; extra "
        "grants here mean the coverage check regressed."
    )


def test_wrap_launcher_argv_target_with_unsafe_parent_degrades_not_raises(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    The target lane must NEVER raise (parity with the crash-instead-
    of-degrade complaint): when the resolved target's parent directory
    is too broad to grant — an unsafe ancestor, ``$HOME``, or above —
    the wrap degrades to a literal read on the binary plus an audit
    WARNING instead of failing the spawn.
    """
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    binary = fake_home / "claude"
    binary.write_bytes(b"\x00fakebun")
    binary.chmod(0o755)
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    backend = _make_backend()
    policy = _make_policy(cwd, allow_hidden=[".venv"])
    with patch(
        "omnigent.inner.seatbelt_sandbox._UNSAFE_WIDEN_ANCESTORS",
        frozenset({str(fake_home)}),
    ):
        with caplog.at_level("WARNING", logger="omnigent.inner.seatbelt_sandbox"):
            argv = backend.wrap_launcher_argv(
                _safe_helper_argv(cwd), policy, cwd, target=str(binary)
            )
    profile = Path(argv[2]).read_text()

    assert f"(allow file-read* (literal {_quote(str(binary))}))" in profile, (
        "The degraded path must still grant a literal read on the "
        "binary itself — best effort beats a guaranteed crash."
    )
    assert f"(allow file-read* (subpath {_quote(str(fake_home))}))" not in profile, (
        "The refused parent must NOT get a subpath grant — that's the "
        "sandbox-defeating widening the guard exists to prevent."
    )
    assert any("too broad" in record.message for record in caplog.records), (
        "The degrade must be auditable via a WARNING naming the refused parent."
    )


def test_symlink_hop_literals_survives_cycles(tmp_path: Path) -> None:
    """
    A symlink cycle (``a -> b -> a``) must terminate via the
    visited-set (same contract as the bwrap walk) and still report
    the symlinks it saw.
    """
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)

    literals = _symlink_hop_literals(a, [])

    assert a in literals and b in literals, (
        f"Cycle members should each be collected once; got {literals!r}."
    )
    assert len(literals) == 2, f"Cycle must not duplicate entries or spin; got {literals!r}."


@pytest.mark.skipif(sys.platform != "darwin", reason="darwin_seatbelt requires macOS")
def test_run_launcher_spawn_wrap_private_tmpdir_boots_under_seatbelt(
    tmp_path: Path,
) -> None:
    """
    End-to-end regression for the spawn-time-wrap private-tmpdir grant.

    Drives the FULL ``create_exec_launcher`` -> ``run_launcher``
    two-pass re-exec (the host pass builds the ``sandbox-exec`` wrap and
    ``execvp``s into it; the in-wrap pass activates and runs the target).
    This is the path claude-sdk / terminals / the other spawn-wrap
    harnesses use — distinct from the direct ``wrap_launcher_argv``
    calls the rest of this file exercises.

    Reproduces the reported failure: inside the wrap, the launcher mints
    a private scratch tmpdir via ``tempfile.mkdtemp()``, which targets
    ``$TMPDIR``. On macOS ``$TMPDIR`` is the system tempdir *root*
    (``/var/folders/.../T``), and the seatbelt profile — baked before
    the re-exec — only grants a *subpath* of it, so the in-wrap
    ``mkdtemp`` died with ``FileNotFoundError: No usable temporary
    directory``. (bwrap masked this via its ``--tmpfs /tmp`` fallback,
    so Linux CI never saw it.) The fix mints + grants the scratch dir on
    the host BEFORE the wrap and hands it to the in-wrap pass, so
    ``mkdtemp`` lands in a profile-granted, writable location.

    The target is a tiny script that calls ``tempfile.mkdtemp()`` and
    writes a file into it — the exact operation that failed. We force
    ``TMPDIR`` to the system tempdir root to reproduce the macOS
    condition deterministically. Pre-fix: the in-wrap launcher exits
    non-zero with the ``FileNotFoundError`` before the target ever runs.
    Post-fix: the target runs and prints ``TMPOK``.
    """
    import os
    import shutil as _shutil
    import subprocess
    import tempfile

    from omnigent.inner.sandbox import _project_root, create_exec_launcher

    if _shutil.which("sandbox-exec") is None:
        pytest.skip("sandbox-exec not on PATH")

    cwd = tmp_path
    # A target that exercises the scratch tmpdir the way real helpers do:
    # mkdtemp() (honours $TMPDIR) then write into it. This is what raised
    # FileNotFoundError inside the wrap pre-fix.
    target = cwd / "tmp_probe.py"
    target.write_text(
        "import tempfile, pathlib\n"
        "d = tempfile.mkdtemp()\n"
        "p = pathlib.Path(d) / 'scratch.txt'\n"
        "p.write_text('ok')\n"
        "assert p.read_text() == 'ok'\n"
        "print('TMPOK', d)\n"
    )

    # cwd is READ-ONLY — the seatbelt default, and the condition that
    # makes this a real reproduction: with no writable cwd, Python's
    # tempfile fallback chain ($TMPDIR -> /tmp -> ... -> os.getcwd())
    # has NO writable entry, so a private-tmpdir mkdtemp that targets an
    # un-granted $TMPDIR fails outright. (If cwd were writable, mkdtemp
    # would silently fall back to it and mask the bug.) ``.venv`` allowed
    # so the dotfile mask doesn't fight the interpreter symlink
    # convention. The project root is a read root so the inline re-exec
    # can import ``omnigent.inner`` — in production that import is
    # covered by the interpreter install-root grant or by cwd; here cwd
    # is pytest's tmp_path, so grant it explicitly.
    policy = _make_policy(
        cwd.resolve(strict=False),
        allow_hidden=[".venv"],
        read_roots=[_project_root()],
    )

    # The launcher script re-execs run_launcher under sandbox-exec. Its
    # shebang is ``sys.executable``; run it as a plain argv so the
    # two-pass flow (host -> execvp sandbox-exec -> in-wrap) happens for
    # real. ``target`` is the interpreter here, exec'd inside the jail.
    launcher = create_exec_launcher(str(Path(sys.executable).resolve(strict=True)), policy)
    try:
        env = dict(os.environ)
        # Reproduce the macOS failure condition explicitly: $TMPDIR is
        # the system tempdir ROOT, which the profile only grants a
        # subpath of. Pre-fix this is exactly what broke the in-wrap
        # mkdtemp.
        env["TMPDIR"] = tempfile.gettempdir()
        result = subprocess.run(
            [launcher, str(target)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    finally:
        os.unlink(launcher)

    assert "No usable temporary directory" not in result.stderr, (
        "In-wrap mkdtemp could not find a writable $TMPDIR — the private "
        "scratch tmpdir was not granted in the baked seatbelt profile. "
        f"rc={result.returncode}\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    assert result.returncode == 0, (
        f"Wrapped launcher exited {result.returncode}.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "TMPOK" in result.stdout, (
        "Target ran but didn't reach the scratch-write marker; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
