"""Interactive harness & credential configuration for the CLI.

Everything behind ``omnigent config`` / ``omnigent setup`` and the first-run
``configure harnesses`` picker: detecting installed provider CLIs, prompting for
API keys, installing SDK harnesses, and writing the results to global config.
Extracted from :mod:`omnigent.cli` to keep that module under the repo's
per-file size budget; the ``config`` / ``setup`` Click commands stay in
``omnigent.cli`` and reach these helpers via :func:`register`.

The three config-load helpers these functions need (``_load_global_config``,
``_save_global_config``, ``_load_effective_config``) live in ``omnigent.cli`` and
are used ~20x each there, so they stay put; this module reaches them through
call-time proxies below, so importing this module never imports ``omnigent.cli``
(no cycle) and a test's ``monkeypatch`` of ``omnigent.cli.<helper>`` is still
honoured.
"""

from __future__ import annotations

import collections.abc
import contextlib
import json
import os
import shutil
import subprocess
import types
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from omnigent.inner import ui
from omnigent.onboarding.ucode_setup import (
    build_ucode_configure_command,
    find_ucode_command,
    model_gateway_workspace_urls,
)

if TYPE_CHECKING:
    from omnigent._runner_startup import RunnerStartupProgress
    from omnigent.onboarding.ambient import DetectedProvider
    from omnigent.onboarding.provider_config import ProviderEntry

# _INTERNAL_BETA_DEFAULT_SERVER (internal Databricks Apps host) moved to
# omnigent.onboarding.internal_beta (excluded from the OSS build); the
# internal-beta setup branch and the sandbox CLI import it from there.
# Brand shown for an auto-configured CLI login in the credentials callout —
# the product the login authenticates, not the CLI name (the codex CLI logs in
# a ChatGPT subscription). Keyed by the ambient detection name; these are the
# only two subscription CLIs ambient detection emits.
_CLI_LOGIN_BRAND: dict[str, str] = {"claude": "Claude", "codex": "ChatGPT"}


def _load_global_config(*a, **k):  # type: ignore[no-untyped-def]
    import omnigent.cli as _cli

    return _cli._load_global_config(*a, **k)


def _save_global_config(*a, **k):  # type: ignore[no-untyped-def]
    import omnigent.cli as _cli

    return _cli._save_global_config(*a, **k)


def _load_effective_config(*a, **k):  # type: ignore[no-untyped-def]
    import omnigent.cli as _cli

    return _cli._load_effective_config(*a, **k)


# Node version hint shared by the preflight problem messages and surfaced
# to the user. The Node-based harness CLIs (Claude Code, Codex, Pi) bundle
# a copy of ``undici`` that calls ``worker_threads.markAsUncloneable`` — a
# Node API added in 22.10 that is absent from every 20.x release. On older
# Node it surfaces as the opaque
# ``TypeError: webidl.util.markAsUncloneable is not a function``.
_NODE_MIN_VERSION_HINT = "Node.js 22 LTS or newer (a 22.10+ API is required)"


def _node_version(node_path: str) -> str | None:
    """
    Return the ``node --version`` string (e.g. ``v20.12.2``) or ``None``.

    Used only to make the "too old" warning concrete; a failure to read the
    version is non-fatal — the caller still reports the underlying problem.

    :param node_path: Absolute path to the ``node`` binary, as resolved by
        :func:`shutil.which`.
    :returns: The trimmed version string, or ``None`` if ``node`` could not
        be invoked.
    """
    try:
        result = subprocess.run(
            [node_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _node_dependency_problem() -> str | None:
    """
    Return a one-line problem if Node is missing or too old, else ``None``.

    The Node-based harnesses (``claude-native``, ``codex``, ``pi``) shell
    out to CLIs that bundle ``undici``; that bundle calls
    ``worker_threads.markAsUncloneable`` (added in Node 22.10). We invoke
    ``node`` to probe for the symbol directly rather than parse
    ``node --version``, so the check tracks the actual capability across
    the 22.x/23.x version split and never goes stale against a hardcoded
    floor.

    :returns: A human-readable description suitable for a warning bullet,
        or ``None`` when Node is present and new enough. A flaky/timed-out
        probe also yields ``None`` — setup should not block on it.
    """
    node = shutil.which("node")
    if node is None:
        return f"node not found — Claude, Codex, and Pi need {_NODE_MIN_VERSION_HINT}."
    # Probe the exact API the bundled undici calls. Exit 0 ⇒ capability
    # present; exit 1 ⇒ too old; we treat any other failure as inconclusive.
    probe = (
        "process.exit("
        "typeof require('node:worker_threads').markAsUncloneable === 'function' ? 0 : 1)"
    )
    try:
        result = subprocess.run(
            [node, "-e", probe],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return None
    version = _node_version(node)
    detected = f" (detected {version})" if version else ""
    return f"Node.js is too old{detected} — Claude, Codex, and Pi need {_NODE_MIN_VERSION_HINT}."


@contextlib.contextmanager
def _isolated_databricks_cfg() -> collections.abc.Generator[None, None, None]:
    """Run Databricks setup against a temp config containing only our three profiles.

    The temp file starts with just the canonical internal-beta profile
    sections (see ``DEFAULT_PROFILES``) seeded from the original when they
    exist, so there is exactly one section per workspace host and
    ``databricks auth token --host X`` never hits the "multiple profiles
    match" ambiguity error.

    The user's real config is never modified while this context is active.
    On normal exit the three sections are merged back into the original.
    On SIGTERM / SIGINT the temp file is removed and the original is left
    exactly as it was.  SIGKILL cannot be caught, but the original is
    always safe because we never touch it.

    Uses ``DATABRICKS_CONFIG_FILE`` so both subprocess CLI calls *and*
    the direct configparser writes in ``omnigent.onboarding.setup``
    (via ``_databrickscfg_path()``) all operate on the temp file. Also
    strips every entry in ``CONFLICTING_ENV_VARS`` for the duration of
    the context so a stale Databricks credential env var (see that list)
    can't shadow ``--host`` inside ``databricks auth token``.
    """
    import configparser
    import signal
    import tempfile

    from omnigent.onboarding.internal_beta import DEFAULT_PROFILES
    from omnigent.onboarding.setup import CONFLICTING_ENV_VARS

    original_cfg = Path.home() / ".databrickscfg"
    saved_env: dict[str, str | None] = {
        "DATABRICKS_CONFIG_FILE": os.environ.get("DATABRICKS_CONFIG_FILE"),
    }
    for var in CONFLICTING_ENV_VARS:
        saved_env[var] = os.environ.pop(var, None)

    def _restore_env() -> None:
        for var, prev in saved_env.items():
            if prev is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = prev

    # Temp file contains only the canonical internal-beta profile sections
    # (see DEFAULT_PROFILES), seeded from the original when they already
    # exist. Everything else is excluded so there is exactly one
    # section per workspace host and `databricks auth token --host X`
    # never hits the "multiple profiles match" ambiguity error.
    orig_cfg = configparser.ConfigParser()
    if original_cfg.exists():
        orig_cfg.read(original_cfg)
    cfg = configparser.ConfigParser()
    for spec in DEFAULT_PROFILES:
        if orig_cfg.has_section(spec.name):
            cfg[spec.name] = dict(orig_cfg[spec.name])

    omnigent_dir = Path.home() / ".omnigent"
    omnigent_dir.mkdir(exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix="databrickscfg-setup-",
        dir=omnigent_dir,
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            cfg.write(f)
    except Exception:
        os.unlink(tmp_name)
        raise
    tmp_path = Path(tmp_name)

    os.environ["DATABRICKS_CONFIG_FILE"] = tmp_name

    def _on_signal(signum: int, _frame: types.FrameType | None) -> None:
        tmp_path.unlink(missing_ok=True)
        _restore_env()
        # Restore the original handler before re-raising so signal chaining
        # (e.g. Click's Ctrl-C → Abort) is preserved rather than falling
        # back to SIG_DFL which would kill the process through the OS.
        signal.signal(signum, prev_sigterm if signum == signal.SIGTERM else prev_sigint)
        signal.raise_signal(signum)

    prev_sigterm = signal.signal(signal.SIGTERM, _on_signal)
    prev_sigint = signal.signal(signal.SIGINT, _on_signal)

    write_tmp: Path | None = None
    try:
        yield
        # Merge canonical sections written by setup back into the real cfg.
        tmp_cfg = configparser.ConfigParser()
        tmp_cfg.read(tmp_path)
        orig_cfg = configparser.ConfigParser()
        if original_cfg.exists():
            orig_cfg.read(original_cfg)
        for spec in DEFAULT_PROFILES:
            if tmp_cfg.has_section(spec.name):
                orig_cfg[spec.name] = dict(tmp_cfg[spec.name])
        write_tmp = original_cfg.with_suffix(".tmp")
        with write_tmp.open("w") as f:
            orig_cfg.write(f)
        write_tmp.replace(original_cfg)
        write_tmp = None
    finally:
        tmp_path.unlink(missing_ok=True)
        if write_tmp is not None:
            write_tmp.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, prev_sigterm)
        signal.signal(signal.SIGINT, prev_sigint)
        _restore_env()


def _run_configure_databricks() -> None:
    """
    Configure coding harnesses to use Databricks Unity AI Gateway.

    Shells out to ``ucode configure`` to authenticate workspaces and set
    up harnesses (Claude SDK, Codex, OpenAI Agents, Pi). After setup,
    Omnigent reads ``~/.ucode/state.json`` to pick per-harness model
    defaults and base URLs.

    :returns: None.
    :raises click.ClickException: If ucode command resolution,
        configuration, or state verification fails.
    """
    ucode_command = find_ucode_command()
    # ucode only configures the model-serving gateway, so it gets the
    # gateway workspace(s) only — not the MCP-only profiles, which are
    # authenticated during profile onboarding and have no ucode role.
    workspace_urls = model_gateway_workspace_urls()
    click.echo("Running `ucode configure --workspaces ...`...")

    result = subprocess.run(
        build_ucode_configure_command(ucode_command, workspace_urls=workspace_urls),
        check=False,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"`ucode configure` exited with code {result.returncode}; "
            "see the command output above for details."
        )

    click.echo("ucode configuration complete. Omnigent will use state.json for harness setup.")


def _warn_missing_harness_dependencies() -> None:
    """
    Warn about external (non-Python) tools the coding harnesses need.

    Surfaces every missing/outdated dependency up front (when the user
    opens ``configure harnesses``) so a fresh machine learns about all of
    them at once, rather than discovering each at the moment a harness or
    wrapper needs it (Node when a harness CLI runs, tmux when ``omnigent
    claude`` launches). This *warns* rather than aborts on purpose: the
    pure-Python ``openai-agents`` harness runs without either tool, so a
    hard failure would block a valid flow — but ``omnigent claude`` /
    ``codex`` do need both, hence the prominent notice.

    :returns: None. Side effect: writes a yellow warning block to stderr
        via :mod:`omnigent.inner.ui` when one or more dependencies are
        missing.
    """
    problems: list[str] = []
    node_problem = _node_dependency_problem()
    if node_problem is not None:
        problems.append(node_problem)
    if shutil.which("tmux") is None:
        problems.append(
            "tmux not found — native Claude/Codex need tmux (macOS: `brew install tmux`)."
        )
    if not problems:
        return
    ui.warn("Some harnesses need external tools:")
    for problem in problems:
        ui.err_console.print(f"  • {problem}", style="omni.warning", markup=False)
    ui.err_console.print(
        "You can configure credentials now; install these before launching those harnesses.",
        style="omni.warning",
        markup=False,
    )


def _print_credentials_by_harness() -> None:
    """Print configured model credentials grouped by harness (the ``config list`` view).

    Renders the effective config **merged with ambient detections** (a
    detected env key / CLI login shows as an ordinary credential, with no
    separate "detected vs configured" split) grouped under each harness
    family, with the per-family default marked — via
    :func:`render_provider_listing_by_harness`.

    :returns: None. Side effect: writes the listing to the onboarding
        console.
    """
    from omnigent.onboarding.configure_models import render_provider_listing_by_harness
    from omnigent.onboarding.detected import effective_config_with_detected
    from omnigent.onboarding.provider_config import load_providers

    config = effective_config_with_detected(_load_effective_config())
    providers = load_providers(config)
    render_provider_listing_by_harness(config, providers)


def _existing_key_name_for_ref(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
    api_key_ref: str,
) -> str | None:
    """Return the name of a ``key`` provider on *family* using *api_key_ref*.

    Two API keys are "the same key" when they read the same secret source
    (the same ``env:`` / ``keychain:`` reference). The add flow uses this to
    update such a key in place rather than writing a second, identical entry —
    so re-adding a key you already have stays idempotent, while a key from a
    genuinely different source gets its own entry (the "keep both" behavior).

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family the key serves, ``"anthropic"`` or
        ``"openai"``.
    :param api_key_ref: The secret reference to match, e.g.
        ``"env:ANTHROPIC_API_KEY"`` or ``"keychain:anthropic"``.
    :returns: The provider name whose *family* block references the same
        secret, e.g. ``"anthropic"``, or ``None`` when no such key exists.
    """
    from omnigent.onboarding.provider_config import KEY_KIND, load_providers

    for name, entry in load_providers(config).items():
        if entry.kind != KEY_KIND:
            continue
        fam = entry.families.get(family)
        if fam is not None and fam.api_key_ref == api_key_ref:
            return name
    return None


def _unique_provider_name(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    candidate: str,
) -> str:
    """Return *candidate*, suffixed numerically until it's a free provider name.

    Provider names key the ``providers:`` mapping, so a colliding name would
    overwrite an existing entry on deep-merge. When the add flow keeps a
    second credential (an API key from a new source for a vendor that already
    has one), this derives a fresh name — ``anthropic`` → ``anthropic-2`` →
    ``anthropic-3`` — so both coexist.

    :param config: The parsed global config mapping (``providers:`` block).
    :param candidate: The preferred name, e.g. ``"anthropic"``.
    :returns: *candidate* if unused, else the first free ``<candidate>-<n>``
        (``n`` starting at 2), e.g. ``"anthropic-2"``.
    """
    from omnigent.onboarding.provider_config import load_providers

    existing = set(load_providers(config))
    if candidate not in existing:
        return candidate
    n = 2
    while f"{candidate}-{n}" in existing:
        n += 1
    return f"{candidate}-{n}"


def _resolve_key_provider_name(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
    candidate: str,
    api_key_ref: str,
) -> str:
    """Pick the entry name for an API key being added — update vs keep-both.

    Realizes the "allow multiple API keys, keep both if source differs"
    behavior: a key whose secret source (*api_key_ref*) matches an existing
    key on *family* reuses that entry's name (an in-place update of the same
    credential); a key from a new source takes a fresh, unique name so it
    coexists with the others.

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family the key serves, ``"anthropic"`` or
        ``"openai"``.
    :param candidate: The preferred name (the vendor id for a preset, or the
        user-typed name for "Other provider"), e.g. ``"anthropic"``.
    :param api_key_ref: The key's secret reference, e.g.
        ``"env:ANTHROPIC_API_KEY"`` or ``"keychain:anthropic"``.
    :returns: The existing same-source entry's name (update in place), else a
        unique name derived from *candidate* (keep both), e.g.
        ``"anthropic-2"``.
    """
    same_source = _existing_key_name_for_ref(config, family, api_key_ref)
    if same_source is not None:
        return same_source
    return _unique_provider_name(config, candidate)


def _credential_source_hint(entry: ProviderEntry, family: str) -> str | None:
    """A short, non-secret descriptor of where a key's secret comes from.

    Used to disambiguate two API keys that would otherwise share a label
    (e.g. two "Anthropic API Key" rows): an ``env:`` ref renders as
    ``$VAR``, a ``keychain:`` ref as its stored name, an inline ``$VAR`` as
    itself. Only meaningful for credential kinds that carry an inline family
    block (``key`` / ``gateway`` / ``local``).

    :param entry: The parsed provider entry.
    :param family: The surface whose secret source to describe,
        ``"anthropic"``, ``"openai"``, or ``"pi"``.
    :returns: A display hint such as ``"$ANTHROPIC_API_KEY"`` or
        ``"anthropic-2"``, or ``None`` when the family has no resolvable
        source descriptor.
    """
    from omnigent.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        OPENAI_FAMILY,
        PI_SURFACE,
    )

    raw = entry.families.get(family)
    if raw is None and family == PI_SURFACE:
        # The pi surface carries no family block of its own — pi consumes
        # the credential of whichever family it routes through (anthropic
        # preferred), so describe that family's source instead.
        for fam in (ANTHROPIC_FAMILY, OPENAI_FAMILY):
            raw = entry.families.get(fam)
            if raw is not None:
                break
    if raw is None:
        return None
    if raw.api_key_ref is not None:
        if raw.api_key_ref.startswith("env:"):
            return f"${raw.api_key_ref[len('env:') :]}"
        if raw.api_key_ref.startswith("keychain:"):
            return raw.api_key_ref[len("keychain:") :]
    if raw.api_key is not None and raw.api_key.startswith("$"):
        return raw.api_key
    return None


def _family_key_count(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
) -> int:
    """Count the ``key`` providers serving *family*.

    The ``($VAR)`` disambiguation hint is shown only when more than one API
    key serves a harness — a lone key needs no source qualifier.

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family, ``"anthropic"`` or ``"openai"``.
    :returns: The number of ``kind: key`` providers serving *family*.
    """
    from omnigent.onboarding.provider_config import (
        KEY_KIND,
        load_providers,
        provider_families,
    )

    return sum(
        1
        for entry in load_providers(config).values()
        if entry.kind == KEY_KIND and family in provider_families(entry)
    )


def _family_credential_label(  # type: ignore[explicit-any]  # config is a yaml-boundary mapping
    config: dict[str, Any],
    family: str,
    name: str,
    entry: ProviderEntry,
) -> str:
    """A credential label, qualified with its source when keys would collide.

    Wraps :func:`_credential_label`, appending the ``($VAR)`` source hint for
    a ``key`` provider when more than one API key serves *family* (so two
    "Anthropic API Key" rows read as distinct). Non-key kinds and the
    single-key case render the plain label.

    :param config: The parsed global config mapping (``providers:`` block).
    :param family: The harness family in context, ``"anthropic"`` /
        ``"openai"``.
    :param name: The provider id keyed under ``providers:``, e.g.
        ``"anthropic-2"``.
    :param entry: The parsed provider entry.
    :returns: A human label, e.g. ``"Anthropic API Key ($ANTHROPIC_API_KEY)"``
        when disambiguation applies, else ``"Anthropic API Key"``.
    """
    from omnigent.onboarding.provider_config import KEY_KIND

    base = _credential_label(name, entry)
    if entry.kind != KEY_KIND or _family_key_count(config, family) <= 1:
        return base
    hint = _credential_source_hint(entry, family)
    return f"{base} ({hint})" if hint else base


def _configure_harness_add(family: str | None = None) -> str | None:
    """Run the interactive ``add a provider`` flow and persist the entry.

    Prompts for the provider kind (key / subscription / gateway /
    databricks), gathers the kind-specific fields, deep-merges the single
    entry under ``providers:`` (an add never rewrites siblings), and makes
    it the default for any family it serves that has **no** default yet
    (so a first provider just works; an existing default is left for the
    user to change by selecting it in the harness tree).

    :param family: When set (``"anthropic"`` / ``"openai"`` / ``"pi"``),
        the add menu is scoped to credentials that can drive that harness —
        the per-harness "Add a provider" path. ``None`` shows the full menu.
    :returns: A confirmation message for the caller to show as a transient
        status. Side effect: writes to ``~/.omnigent/config.yaml`` and,
        for a pasted API key, the secret store.
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.ambient import detect_providers
    from omnigent.onboarding.configure_models import (
        AddOption,
        add_menu_options,
        add_menu_options_for_family,
        build_bedrock_provider_entry,
        build_cli_config_provider_entry,
        build_databricks_provider_entry,
        build_gateway_provider_entry,
        build_key_provider_entry,
        build_subscription_provider_entry,
        default_base_url_for_family,
        family_for_key_provider,
        key_provider_endpoint,
        other_key_providers,
        provider_display_name,
    )
    from omnigent.onboarding.interactive import console, prompt_text, select
    from omnigent.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        BEDROCK_KIND,
        CHAT_WIRE_API,
        CLI_CONFIG_KIND,
        DATABRICKS_KIND,
        OPENAI_FAMILY,
        PI_SURFACE,
        RESPONSES_WIRE_API,
        SUBSCRIPTION_KIND,
        load_providers,
        provider_entry_settings,
        set_default_provider,
    )

    # The ucode agent that backs each harness surface's model serving. When the
    # user adds Databricks from a specific harness page, we configure ucode for
    # ONLY that harness (not all of claude/codex/pi) so ucode touches just the
    # one tool the user is wiring up.
    _FAMILY_UCODE_AGENT = {ANTHROPIC_FAMILY: "claude", OPENAI_FAMILY: "codex", PI_SURFACE: "pi"}

    # A flat, credential-aware menu: the user picks "OpenAI — API key" or
    # "Claude — subscription" directly (rather than a bare kind then
    # provider two-step). Each option carries the resolved kind and, for
    # the common cases, a preset provider/cli. When entered from a specific
    # harness, the menu is scoped to that harness's surface.
    options = add_menu_options_for_family(family) if family is not None else add_menu_options()
    # A custom provider defined by the user's own ~/.codex/config.toml
    # (e.g. isaac's Databricks AI Gateway) that is not currently configured
    # gets its own add option. This is the only way back after Remove —
    # removal dismisses the detection so it stops auto-adopting, and there
    # is nothing to type/paste here (the credential lives in that file).
    cli_config_dets: list[DetectedProvider] = []
    if family in (None, OPENAI_FAMILY):
        configured_names = set(load_providers(_load_global_config()))
        cli_config_dets = [
            d
            for d in detect_providers()
            if d.kind == CLI_CONFIG_KIND and d.name not in configured_names
        ]
    # Base options first, then one row per detected config provider — the
    # selection index maps back into cli_config_dets below.
    base_option_count = len(options)
    options = options + [
        AddOption(
            label=f"\N{GEAR}\N{VARIATION SELECTOR-16} {d.display_name or d.name} — "
            "from your Codex config",
            description=(
                f"Use the {str(d.model_provider)!r} provider your ~/.codex/config.toml "
                "defines and authenticates."
            ),
            kind=CLI_CONFIG_KIND,
        )
        for d in cli_config_dets
    ]
    choice = select(
        "What do you want to add?",
        [o.label for o in options],
        descriptions=[o.description for o in options],
        clear_on_exit=True,
    )
    if choice < 0:  # Esc — abort the add
        return None
    chosen = options[choice]
    kind = chosen.kind

    name: str
    # Any (not object): this entry is handed to provider_entry_settings /
    # set_default_provider, which type their config mappings as object;
    # _ConfigValue would trip dict invariance against those. Matches the
    # cli.py yaml-boundary convention.
    entry: dict[str, Any]  # type: ignore[explicit-any]

    if kind == CLI_CONFIG_KIND:
        # One detected-config row was appended per cli_config_dets entry, in
        # order, after the base options — map the selection back to its
        # detection. Nothing to prompt for: the provider definition AND its
        # credential live in ~/.codex/config.toml; the entry only pins it.
        det = cli_config_dets[choice - base_option_count]
        if det.model_provider is None:  # always set on cli-config detections
            raise click.ClickException("internal: cli-config detection missing model_provider")
        name = det.name
        entry = build_cli_config_provider_entry("codex", det.model_provider, det.display_name)
        # Re-adding is the user saying "I want this auto-detected credential
        # after all" — drop any standing dismissal so it behaves like an
        # ordinary detection again (e.g. re-adopts after a config self-heal).
        _clear_detection_dismissal(name)

    elif kind == "key":
        if chosen.provider is not None:
            provider = chosen.provider  # preset by the flat option (OpenAI/Anthropic/OpenRouter)
            # Preset: the preferred name is the provider id — but the final name
            # is resolved from the key's source below (update in place vs keep
            # both), so a second key for the same vendor doesn't overwrite the
            # first.
            candidate = provider
        else:
            # "Other provider — API key": pick from the remaining catalog,
            # shown by friendly display name. This is the one key case where a
            # custom name is useful (e.g. two configs for the same vendor), so
            # it's the only non-gateway path that still prompts for a name.
            others = other_key_providers()
            if not others:  # ponytail: every catalog key-provider is already a preset/configured
                click.echo("No other API-key providers left to add.")
                return None
            _other_choice = select(
                "Which provider?",
                [provider_display_name(p) for p in others],
                clear_on_exit=True,
            )
            if _other_choice < 0:  # Esc — abort the add
                return None
            provider = others[_other_choice]
            candidate = prompt_text("Name for this provider", default=provider)
        disp = provider_display_name(provider)
        family = family_for_key_provider(provider)
        # The entry name is resolved from the key's source (not just the
        # candidate): a key whose source matches an existing one updates it in
        # place, while a key from a new source takes a fresh name so both
        # coexist ("allow multiple API keys"). See _resolve_key_provider_name.
        config_now = _load_global_config()
        # Offer to reuse a detected env var for this provider rather than
        # forcing the user to re-paste a key they already have in the env.
        detected = {d.name: d for d in detect_providers()}
        api_key_ref: str
        if (
            provider in detected
            and detected[provider].kind == "key"
            and click.confirm(
                f"Detected {detected[provider].source} in the environment — use it?",
                default=True,
            )
        ):
            env_var = detected[provider].source.lstrip("$")  # e.g. "ANTHROPIC_API_KEY"
            api_key_ref = f"env:{env_var}"
            name = _resolve_key_provider_name(config_now, family, candidate, api_key_ref)
        else:
            # A pasted key is stored at keychain:<name>; resolve the name first
            # (an existing key in this same keychain slot is replaced in place,
            # otherwise we pick a free name) so we store under and reference the
            # final name.
            name = _resolve_key_provider_name(
                config_now, family, candidate, f"keychain:{candidate}"
            )
            pasted = prompt_text(f"{disp} API key", hide_input=True)
            secret_store.store_secret(name, pasted)
            api_key_ref = f"keychain:{name}"

        # Default model — free-form text entry. The bundled catalog lags new
        # releases (e.g. a brand-new claude-sonnet-4-6 won't be listed yet), so
        # a fixed picker would block the user from a model they can actually
        # use. Pre-fill the canonical default and let the user type ANY model
        # id. Blank → the default (or no pin when unknown). Always persisting
        # a pin keeps a later re-add from silently dropping ``models.default``.
        from omnigent.onboarding.providers import default_chat_model

        catalog_default = default_chat_model(provider)
        # default=catalog_default (str | None): a known provider pre-fills its
        # default (blank-enter accepts it); an unknown provider has no default,
        # so the user types a model id. ``.strip() or None`` keeps an
        # all-whitespace entry from becoming a bogus pin.
        typed = prompt_text("Default model", default=catalog_default)
        default_model = typed.strip() or None

        # A third-party OpenAI-compatible vendor (OpenRouter, Groq, …) is
        # reached at its OWN base_url and speaks Chat Completions; openai /
        # anthropic use the canonical family endpoint (and openai keeps the
        # Responses default). Using the family default for a vendor sent its
        # traffic to api.openai.com — the reason an OpenRouter key failed.
        endpoint = key_provider_endpoint(provider)
        if endpoint is not None:
            base_url = endpoint.base_url
            key_wire_api: str | None = endpoint.wire_api
        else:
            base_url = default_base_url_for_family(family)
            key_wire_api = None
        entry = build_key_provider_entry(
            family=family,
            base_url=base_url,
            api_key_ref=api_key_ref,
            default_model=default_model,
            wire_api=key_wire_api,
        )

    elif kind == "subscription":
        cli_name = chosen.cli  # preset by the flat option (claude / codex)
        if cli_name is None:
            raise click.ClickException("internal: subscription option missing a cli login")
        from omnigent.onboarding.harness_install import harness_install_spec, harness_login

        login_family = {agent: fam for fam, agent in _FAMILY_UCODE_AGENT.items()}.get(cli_name)
        if login_family is None:
            raise click.ClickException(f"internal: no login family for cli {cli_name!r}")
        spec = harness_install_spec(login_family)
        disp = spec.display if spec is not None else cli_name
        # A harness has at most ONE subscription — the CLI's own login. If one
        # is already configured for this CLI (under any name, including an
        # ambient login adopted as e.g. ``claude``), adding another just
        # duplicates it — the ``claude`` + ``claude-subscription`` bug. Offer to
        # replace the existing one; declining aborts before we touch the login.
        existing_subs = [
            n
            for n, e in load_providers(_load_global_config()).items()
            if e.kind == SUBSCRIPTION_KIND and e.cli == cli_name
        ]
        if existing_subs:
            brand = _CLI_LOGIN_BRAND.get(cli_name, cli_name)
            replace = select(
                f"A {brand} subscription is already configured. Replace it?",
                ["Replace it", "Keep the current one"],
                default=0,
                clear_on_exit=True,
            )
            if replace != 0:  # "Keep the current one" or Esc — abort the add
                return None
        # Configure is the single place to sign in: drive the harness's own
        # login (a no-op if already logged in). Only record the subscription
        # once the CLI is actually authenticated — otherwise we'd persist a
        # phantom subscription that strands the user at the harness's own login
        # screen at run time (the exact bug this whole flow fixes).
        console.print(f"  [dim]Signing in to {disp} (its login will open)…[/dim]")
        if not harness_login(login_family):
            return f"✗ {disp} login not completed — subscription not added"
        # Login succeeded — drop the existing subscription(s) for this CLI so the
        # canonical entry is the only one left (clearing the old default lets the
        # new entry re-claim the family default below). Done AFTER login so a
        # failed login leaves the existing subscription intact.
        if existing_subs:
            block = _load_global_config().get("providers")
            if isinstance(block, dict):
                remaining = {k: v for k, v in block.items() if k not in existing_subs}
                _save_global_config({"providers": remaining})  # wholesale replace
        # Subscription name is derived from the CLI login — no prompt.
        name = f"{cli_name}-subscription"
        entry = build_subscription_provider_entry(cli_name)

    elif kind == "gateway":
        name = prompt_text("Name for this gateway", default="gateway")
        base_url = prompt_text("Gateway base_url (OpenAI/Anthropic-compatible)")
        pasted = prompt_text("Gateway API key", hide_input=True)
        secret_store.store_secret(name, pasted)
        # Which harness surfaces — one clear pick instead of two y/n prompts.
        # (These are *harness* surfaces: Codex/OpenAI → codex + openai-agents;
        # Claude/Anthropic → claude-sdk + native-claude.)
        surface_choice = select(
            "Which harnesses can this gateway drive?",
            [
                "Both Claude and Codex",
                "Codex / OpenAI only (codex, openai-agents)",
                "Claude only (claude-sdk, native-claude)",
            ],
            default=0,
            clear_on_exit=True,
        )
        if surface_choice < 0:  # Esc — abort the add
            return None
        families = (
            [OPENAI_FAMILY, ANTHROPIC_FAMILY]
            if surface_choice == 0
            else [OPENAI_FAMILY]
            if surface_choice == 1
            else [ANTHROPIC_FAMILY]
        )
        # Wire protocol for the OpenAI surface: OpenAI / LiteLLM speak the
        # Responses API; OpenRouter and many OSS-model gateways are
        # Chat-Completions-only. Picking wrong makes every turn fail (the
        # exact "OpenRouter doesn't work but LiteLLM does" symptom), so ask —
        # defaulting to Chat when the URL looks like OpenRouter.
        wire_api: str | None = None
        if OPENAI_FAMILY in families:
            wire_choice = select(
                "OpenAI wire protocol for this gateway?",
                [
                    "Responses API (OpenAI, LiteLLM)",
                    "Chat Completions (OpenRouter, most OSS-model gateways)",
                ],
                default=1 if "openrouter" in base_url.lower() else 0,
                clear_on_exit=True,
            )
            if wire_choice < 0:  # Esc — abort the add
                return None
            wire_api = RESPONSES_WIRE_API if wire_choice == 0 else CHAT_WIRE_API
        # Default model per served surface. A gateway has NO catalog default,
        # so without a pin routing would fall back to a vendor model the
        # gateway can't serve. The OpenAI surface pre-fills a broadly-served
        # OSS default (moonshotai/kimi-k2.6, via the openrouter pin); the
        # user can type any gateway model id.
        from omnigent.onboarding.providers import default_chat_model

        models: dict[str, str] = {}
        if OPENAI_FAMILY in families:
            models[OPENAI_FAMILY] = prompt_text(
                "Default model for the Codex / OpenAI surface",
                default=default_chat_model("openrouter"),
            ).strip()
        if ANTHROPIC_FAMILY in families:
            models[ANTHROPIC_FAMILY] = prompt_text(
                "Default model for the Claude surface (the gateway's Claude model id)"
            ).strip()
        entry = build_gateway_provider_entry(
            base_url=base_url,
            api_key_ref=f"keychain:{name}",
            families=families,
            wire_api=wire_api,
            models=models,
        )

    elif kind == BEDROCK_KIND:
        # Bedrock drives the native Claude terminal in AWS Bedrock mode. It
        # authenticates from AWS_BEARER_TOKEN_BEDROCK in the env at launch
        # (Claude Code ignores apiKeyHelper once Bedrock mode is on), so offer
        # to reference an exported token, else store a pasted one in the keychain.
        name = prompt_text("Name for this Bedrock provider", default="bedrock")
        base_url = prompt_text(
            "Bedrock base_url (regional runtime endpoint, or your Bedrock-compatible gateway)",
            default="https://bedrock-runtime.us-east-1.amazonaws.com",
        )
        if os.environ.get("AWS_BEARER_TOKEN_BEDROCK") and click.confirm(
            "Detected AWS_BEARER_TOKEN_BEDROCK in the environment — use it?", default=True
        ):
            api_key_ref = "env:AWS_BEARER_TOKEN_BEDROCK"
        else:
            pasted = prompt_text("Amazon Bedrock API key (bearer token)", hide_input=True)
            secret_store.store_secret(name, pasted)
            api_key_ref = f"keychain:{name}"
        # Bedrock has no catalog default and Claude's own default model is
        # usually not enabled on a Bedrock account, so pin an explicit id.
        default_model = (
            prompt_text(
                "Default model (Bedrock inference-profile id, e.g. "
                "us.anthropic.claude-opus-4-5-20251101-v1:0)"
            ).strip()
            or None
        )
        family = ANTHROPIC_FAMILY
        entry = build_bedrock_provider_entry(
            base_url=base_url,
            api_key_ref=api_key_ref,
            default_model=default_model,
        )

    else:  # databricks
        # Gate on the `databricks` extra: a `kind: databricks` provider mints
        # workspace OAuth tokens via databricks-sdk at runtime
        # (omnigent/runtime/credentials/databricks.py), and the SDK is no
        # longer a default dependency. Abort before any side effect (the
        # `databricks auth login` browser flow, `ucode configure`) so the
        # user isn't signed into a workspace that routing then can't use.
        from omnigent.onboarding.databricks_config import (
            DATABRICKS_EXTRA_INSTALL_HINT,
            databricks_sdk_installed,
        )

        if not databricks_sdk_installed():
            from rich.markup import escape as _rich_escape

            # The status renders through Text.from_markup, where the literal
            # `[databricks]` in the install command would parse as a tag.
            return (
                "✗ Databricks routing needs the databricks extra — "
                f"{_rich_escape(DATABRICKS_EXTRA_INSTALL_HINT)}"
            )

        # The intro + URL prompt render inline, exactly like every other add
        # flow (the add-menu picker already erased its own frame on exit via
        # `clear_on_exit`) — entering the Databricks option should NOT blank the
        # whole screen. The one clear we keep is *after* the subprocess (below):
        # `databricks auth login` + `ucode configure` print a lot, and the
        # in-place menu redraw we return to can only erase its own frame, so we
        # wipe that leftover output once the login finishes.
        # Ask only for the workspace URL — never a profile name. The flow
        # below authenticates that one workspace and runs `ucode configure`
        # against it, scoped to the harness the user drilled into. This is
        # the one place Omnigent triggers a Databricks CLI / ucode login;
        # it never happens on a bare `run`, so a user who only wants their
        # own provider is never routed through Databricks unexpectedly.
        from omnigent.onboarding.configure_models import family_label
        from omnigent.onboarding.databricks_config import normalize_workspace_url
        from omnigent.onboarding.interactive import clear_screen
        from omnigent.onboarding.setup import login_databricks_workspace
        from omnigent.onboarding.ucode_setup import (
            configure_ucode_for_workspace,
            ucode_workspace_exists,
        )

        _routed = f"{family_label(family)}'s" if family is not None else "your harnesses'"
        console.print(
            f"  [dim]Routes {_routed} model calls through this workspace's "
            "Databricks Unity AI Gateway (via ucode), so usage is governed and "
            "billed there. This signs you into the workspace and runs "
            "`ucode configure` for it.[/dim]"
        )
        workspace_url = prompt_text(
            "Databricks workspace URL (e.g. https://example.cloud.databricks.com)"
        ).strip()
        if not workspace_url:  # blank — abort the add
            return None
        if not workspace_url.startswith(("http://", "https://")):
            workspace_url = f"https://{workspace_url}"
        # Reduce to scheme://host. Users paste the URL from a browser address
        # bar, whose `/browse?o=...` path breaks both the saved profile host
        # and `ucode configure` (the Databricks CLI keys OAuth tokens by host,
        # so a path-laden value yields "no access token").
        normalized_workspace_url = normalize_workspace_url(workspace_url)
        if normalized_workspace_url != workspace_url.rstrip("/"):
            console.print(
                f"  [dim]Using {normalized_workspace_url} — ignored the extra "
                "path from the pasted URL.[/dim]"
            )
        workspace_url = normalized_workspace_url

        # 1. Authenticate the workspace (returns the ~/.databrickscfg profile
        #    name) and 2. run `ucode configure` against it for model serving —
        #    scoped to the harness the user drilled into (or both when added
        #    from the un-scoped menu), so ucode configures only what's needed.
        if family is not None:
            ucode_agents = [_FAMILY_UCODE_AGENT[family]]
        else:
            ucode_agents = sorted(_FAMILY_UCODE_AGENT.values())
        profile = login_databricks_workspace(workspace_url, console=console)
        configure_ucode_for_workspace(workspace_url, agents=ucode_agents)
        # Fail loud if ucode didn't actually record state for the workspace —
        # otherwise routing would silently fall back and confuse the user.
        if not ucode_workspace_exists(workspace_url):
            raise click.ClickException(
                f"`ucode configure` finished but recorded no state for {workspace_url}. "
                "Re-run and check the ucode output above."
            )
        # Wipe the verbose login + ucode output so the menu we return to (with a
        # "✓ Added databricks" status) renders on a clean screen.
        clear_screen()
        # Databricks name is fixed — no prompt. The provider keys on the
        # profile; runtime resolves profile → workspace URL → ucode state.
        name = "databricks"
        entry = build_databricks_provider_entry(profile)

    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.provider_config import (
        provider_families,
        surface_default_provider,
    )

    # Persist the entry (deep-merge — doesn't disturb sibling entries).
    _save_global_config(
        provider_entry_settings(name, entry, make_default=False),
        deep_merge_keys=("providers",),
    )
    # Become the default for any surface it serves that has NO default yet,
    # so a first provider "just works". An existing default is left alone —
    # the user changes defaults by selecting a provider in the harness tree
    # (per-surface, so a shared provider can default one harness, not both).
    # The pi surface checks its *effective* default: a family default already
    # drives pi via the fallback, so claiming the explicit pi scope then
    # would silently re-route pi away from it.
    parsed = load_providers({"providers": {name: entry}})[name]
    # Databricks routing is configured in ucode PER HARNESS (we only ran
    # `ucode configure` for the surface the user drilled into), so it must only
    # become the default for THAT surface — defaulting the other harnesses too
    # would route them through a workspace ucode never configured for them.
    # Other kinds (a gateway serving both families with one base_url + key)
    # still default every surface they serve.
    if entry["kind"] == DATABRICKS_KIND and family is not None:
        default_families = [family]
    else:
        default_families = sorted(provider_families(parsed))
    became_default: list[str] = []
    for fam in default_families:
        cfg = _load_global_config()
        if surface_default_provider(cfg, fam) is not None:
            continue
        block = cfg.get("providers")
        if isinstance(block, dict):
            _save_global_config({"providers": set_default_provider(block, name, fam)})
            became_default.append(fam)
    if became_default:
        labels = " · ".join(family_label(f) for f in became_default)
        return f"✓ Added {name} — default for {labels}"
    return f"✓ Added {name}"


def _adopt_detected_providers() -> list[str]:
    """Persist ambient-detected providers into the config, returning new names.

    Opening ``configure harnesses`` adopts any detected credential (env key,
    CLI login, local Ollama) not already in ``providers:`` as a real,
    editable entry — so the tree shows one uniform provider list with no
    "detected vs configured" split. Writes the merged view (explicit +
    detected, with detected auto-defaulting per family) wholesale, and only
    when there is something new to adopt (idempotent on re-open).

    :returns: The names adopted this call, e.g. ``["anthropic", "codex"]``;
        empty when every detection is already configured.
    """
    from omnigent.onboarding.detected import (
        effective_config_with_detected,
        providers_to_adopt,
    )

    config = _load_global_config()
    to_adopt = providers_to_adopt(config)
    if not to_adopt:
        return []
    merged = effective_config_with_detected(config)
    _save_global_config({"providers": merged["providers"]})  # wholesale replace
    return list(to_adopt)


def _promote_global_auth_to_provider() -> str | None:
    """Backfill a databricks providers entry from an existing global ``auth:`` block.

    Older ``omnigent setup`` runs configured Databricks only via the top-level
    ``auth: {type: databricks}`` block — which ``configure harnesses`` does not
    read — so the readout showed no Databricks provider (and an ambient CLI
    login as the default) even though routing used Databricks. This promotes
    that block into a first-class ``kind: databricks`` providers entry the next
    time ``configure harnesses`` opens, so existing configs self-heal without
    re-running ``omnigent setup``.

    Becomes the default only for families with no existing **provider** default —
    mirroring routing precedence (explicit provider default > ``auth:`` block),
    so an explicitly-chosen default is left untouched while a config that only
    ever had the ``auth:`` block gets Databricks as its default (matching what
    routing already does at runtime). Must run BEFORE
    :func:`_adopt_detected_providers` so Databricks claims the default ahead of
    an ambient CLI login (``auth:`` outranks ambient detection in routing too).

    :returns: ``"databricks"`` if a provider was backfilled, else ``None`` (no
        databricks ``auth:`` block, or a databricks provider already exists).
    """
    from omnigent.onboarding.configure_models import build_databricks_provider_entry
    from omnigent.onboarding.provider_config import (
        load_providers,
        provider_entry_settings,
        provider_families,
        set_default_provider,
        surface_default_provider,
    )

    config = _load_global_config()
    auth = config.get("auth")
    if not isinstance(auth, dict) or auth.get("type") != "databricks":
        return None
    profile = auth.get("profile")
    if not isinstance(profile, str) or not profile:
        return None
    name = "databricks"
    if name in load_providers(config):
        return None  # already a first-class provider — nothing to backfill

    entry = build_databricks_provider_entry(profile)
    _save_global_config(
        provider_entry_settings(name, entry, make_default=False),
        deep_merge_keys=("providers",),
    )
    parsed = load_providers({"providers": {name: entry}})[name]
    for fam in sorted(provider_families(parsed)):
        cfg = _load_global_config()
        # Effective check (matters for the pi surface): a default that
        # already drives the surface — explicitly or via pi's fallback —
        # outranks the legacy auth: block, exactly like routing does.
        if surface_default_provider(cfg, fam) is not None:
            continue  # respect an existing provider default (it outranks auth:)
        block = cfg.get("providers")
        if isinstance(block, dict):
            _save_global_config({"providers": set_default_provider(block, name, fam)})
    return name


def _compact_credential_label(det: DetectedProvider) -> str:
    """A short, brand-qualified label for an auto-configured credential.

    Unlike :func:`omnigent.onboarding.configure_models.credential_label`
    (which renders every CLI login as a bare ``"Subscription"`` because a
    harness only ever has one), this names the *brand* behind a login —
    ``"Claude Subscription"`` / ``"ChatGPT Subscription"`` — so a single
    comma-joined callout listing several credentials at once stays unambiguous
    without a per-line source. API keys and local endpoints reuse the shared
    ``credential_label`` (``"Anthropic API Key"``, ``"Ollama"``).

    :param det: A credential found by
        :func:`omnigent.onboarding.ambient.detect_providers`.
    :returns: A short human label, e.g. ``"Anthropic API Key"``,
        ``"Claude Subscription"``, or ``"ChatGPT Subscription"``.
    """
    from omnigent.onboarding.ambient import SUBSCRIPTION_KIND
    from omnigent.onboarding.configure_models import credential_label

    if det.kind == SUBSCRIPTION_KIND:
        # Fallback to the raw CLI name is unreachable for today's detections
        # (see _CLI_LOGIN_BRAND) but keeps an added CLI readable, not crashing.
        brand = _CLI_LOGIN_BRAND.get(det.name, det.name)
        return f"{brand} Subscription"
    # A cli-config detection carries the provider's own display name
    # ("Databricks AI Gateway"); other kinds ignore the keyword.
    return credential_label(det.kind, det.name, display_name=det.display_name)


def _announce_auto_configured_credentials(adopted: list[str]) -> None:
    """Print the "found existing credentials → auto-configured" callout.

    Re-runs ambient detection to recover each adopted credential, then prints a
    single compact, dimmed line naming them inline (e.g. ``Anthropic API Key,
    Claude Subscription, ChatGPT Subscription``) — so a user who never ran an
    explicit setup sees, the first time we auto-configure, exactly which
    credentials omnigent picked up (rather than silently inheriting them).
    Styled ``dim`` rather than the onboarding accent so it reads as a quiet
    notice, not a prominent header.

    :param adopted: Provider names just persisted by
        :func:`_adopt_detected_providers`, e.g. ``["anthropic", "codex"]``.
        A name with no matching live detection is skipped (defensive — the
        adopt set and the detection list come from the same detection pass, so
        in practice every name resolves).
    :returns: None. Side effect: writes the callout to the shared onboarding
        console (stdout). Prints nothing when no adopted name resolves to a
        live detection.
    """
    from omnigent.onboarding.ambient import detect_providers
    from omnigent.onboarding.interactive import console

    detected = {det.name: det for det in detect_providers()}
    labels = [_compact_credential_label(detected[name]) for name in adopted if name in detected]
    if not labels:
        return
    console.print(
        "\n[dim]Found existing credentials on your machine, "
        f"auto-configured for omnigent: {', '.join(labels)}[/dim]"
    )


def _adopt_ambient_credentials(progress: RunnerStartupProgress | None = None) -> list[str]:
    """Self-heal config, adopt ambient credentials, and announce what was added.

    The shared front half of both a bare ``omnigent run``'s first-run path
    (:func:`_resolve_first_run_plan`) and the ``configure harnesses`` picker
    (:func:`_run_configure_harnesses_interactive`): it (1) backfills a legacy
    databricks ``auth:`` block into a real provider, (2) adopts any
    ambient-detected credential (env API key, logged-in ``claude`` / ``codex``
    CLI, local Ollama) not already configured as an ordinary provider entry,
    and (3) prints a callout naming exactly the credentials it just
    auto-configured. Idempotent: a second open adopts nothing, so no callout
    prints.

    The callout is scoped to *machine* credentials — the ambient detections —
    not the databricks ``auth:`` backfill, which promotes an existing config
    block rather than something newly "found on your machine".

    :param progress: Optional spinner handle (from
        :func:`omnigent._runner_startup.runner_startup_progress`) covering the
        detection step — slow on macOS, where Claude detection now shells out to
        ``claude auth status`` to read the Keychain. When supplied, it is
        ``finish()``-ed (the spinner cleared) right before the callout prints,
        so the "Found existing credentials…" line is not clobbered by the
        animating spinner. ``None`` (the ``run`` first-run path) means no
        spinner — behavior is unchanged.
    :returns: The provider names adopted this call, e.g. ``["anthropic"]``;
        empty when every detection was already configured.
    """
    _promote_global_auth_to_provider()
    adopted = _adopt_detected_providers()
    # Clear the search spinner (if any) before printing — the callout writes to
    # stdout while the spinner animates on stderr, and on a shared TTY the two
    # would otherwise overwrite each other.
    if progress is not None:
        progress.finish()
    if adopted:
        _announce_auto_configured_credentials(adopted)
    return adopted


@dataclass(frozen=True)
class _HarnessMenuRow:
    """One selectable row in a harness's provider-management menu (level 2).

    :param label: Display text, e.g. ``"🔑 anthropic   ✓ default"``.
    :param action: The action on Enter — ``"set_default"`` / ``"add"`` /
        ``"remove"`` / ``"back"``.
    :param provider: For ``set_default``, the provider name to default;
        ``None`` for the other actions.
    """

    label: str
    action: str
    provider: str | None = None


_SOFT_INSTALL_ABORT = "\x00soft-install-abort"


def _credential_label(name: str, entry: ProviderEntry) -> str:
    """A friendly, jargon-free label for a configured credential.

    A logged-in CLI reads as ``"Subscription"`` (within a harness there is only
    one, so the plan name adds no information); an API-key provider names the
    vendor and the credential type (``"Anthropic API Key"`` / ``"OpenAI API
    Key"``); Databricks as ``"Databricks (<profile>)"``; a gateway / local
    endpoint as its display name — so menus and summaries avoid raw provider
    ids and the word "provider".

    :param name: The provider id keyed under ``providers:``, e.g. ``"openai"``.
    :param entry: The parsed provider entry.
    :returns: A human label, e.g. ``"Anthropic API Key"`` or ``"Databricks (oss)"``.
    """
    from omnigent.onboarding.configure_models import credential_label

    return credential_label(
        entry.kind, name, profile=entry.profile, display_name=entry.display_name
    )


def _harness_credential_rows(config: dict[str, Any], family: str) -> list[_HarnessMenuRow]:  # type: ignore[explicit-any]
    """Build the level-2 rows: each credential serving *family*, then ``+ Add``.

    Each credential row drills into level 3 (make default / remove). The
    current default is marked with a green ✓. ``+ Add a credential`` runs the
    add flow; ``← Back`` returns to the harness picker (as do Esc / ``q``).

    :param config: The parsed config mapping (``providers:`` block).
    :param family: The harness surface being managed.
    :returns: The ordered, all-selectable rows.
    """
    from omnigent.onboarding.configure_models import kind_glyph
    from omnigent.onboarding.provider_config import (
        load_providers,
        provider_families,
        surface_default_provider,
    )

    serving = [
        (name, entry)
        for name, entry in load_providers(config).items()
        if family in provider_families(entry)
    ]
    # The surface's effective default (for pi: explicit scope, else fallback)
    # so the ✓ always marks the credential the harness would actually use.
    default = surface_default_provider(config, family)
    rows: list[_HarnessMenuRow] = []
    for name, entry in serving:
        glyph = kind_glyph(entry.kind)
        cred = _family_credential_label(config, family, name, entry)
        # The current default renders bold-green with a ✓ so it stands out in
        # the list; the rest are plain. Provider names are markup-safe in
        # practice (same assumption select() already makes for every label).
        if default is not None and name == default.name:
            label = f"[bold green]{glyph} {cred}  ✓ default[/]"
        else:
            label = f"{glyph} {cred}"
        rows.append(_HarnessMenuRow(label, action="credential", provider=name))
    rows.append(_HarnessMenuRow("+ Add a credential", action="add"))
    rows.append(_HarnessMenuRow("← Back", action="back"))
    return rows


def _prompt_install_harness(family: str) -> bool:
    """Offer to install an uninstalled harness CLI; return whether to proceed.

    Shown when the user drills into a harness whose CLI isn't on PATH. Offers
    three choices: install it now (``npm install -g …``), go back, or print the
    command to run manually.

    :param family: The harness surface being configured (``"anthropic"`` /
        ``"openai"`` / ``"pi"``).
    :returns: ``True`` only when the CLI is installed afterward (user chose
        install and it succeeded), so the caller continues to credential
        configuration; ``False`` when the user declines, asks to run it
        themselves, the install fails, or they Esc — the caller returns to the
        harness picker.
    """
    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.harness_install import (
        harness_install_command,
        install_harness_cli,
    )
    from omnigent.onboarding.interactive import console, select

    label = family_label(family)
    cmd = " ".join(harness_install_command(family))
    choice = select(
        f"{label}'s CLI isn't installed. Install it now?",
        [
            f"Yes — install ({cmd})",
            "No — back to harnesses",
            "I'll run it myself (show the command)",
        ],
        descriptions=[
            f"Runs `{cmd}` (needs npm), then continues to credential setup.",
            "Return to the harness picker without installing.",
            "Print the command so you can install it yourself, then return.",
        ],
        default=0,
        clear_on_exit=True,
    )
    if choice == 0:
        console.print(f"  [dim]Installing {label} — running `{cmd}`…[/dim]")
        if install_harness_cli(family):
            console.print(f"  [green]✓ {label} installed[/green]")
            return True
        console.print(
            f"  [red]Install failed.[/red] Run it manually, then re-open: [bold]{cmd}[/bold]"
        )
        return False
    if choice == 2:  # run it yourself
        console.print(f"  Install {label} with:\n    [bold]{cmd}[/bold]")
    return False


def _manage_harness_providers(family: str) -> None:
    """Run the level-2 loop for one harness: pick a credential or add one.

    Selecting a credential opens level 3 (make default / remove); ``+ Add``
    runs the add flow. Esc (TTY) / ``q`` (fallback) returns to the harness
    picker. The menu re-renders (cleared in place) after each action so the
    session stays on one tidy screen.

    :param family: The harness family being managed.
    :returns: None.
    """
    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.harness_install import harness_cli_installed
    from omnigent.onboarding.interactive import select

    # If the harness CLI isn't installed, offer to install it before showing
    # the credential menu. Declining (or copy-the-command) returns to the
    # harness picker — there's nothing to configure for a harness you can't run.
    if not harness_cli_installed(family) and not _prompt_install_harness(family):
        return

    # Carry the prior action's confirmation as a transient status line so the
    # menu shows only the latest result — not an accumulating stack of "✓ …".
    status: str | None = None
    while True:
        rows = _harness_credential_rows(_load_global_config(), family)
        idx = select(
            f"{family_label(family)} — select or add a credential",
            [r.label for r in rows],
            clear_on_exit=True,
            status=status,
        )
        if idx < 0:  # Esc / q — back to the harness picker
            return
        row = rows[idx]
        if row.action == "back":
            return
        if row.action == "add":
            status = _configure_harness_add(family=family)
        elif row.action == "credential" and row.provider is not None:
            status = _manage_credential(row.provider, family)


def _prompt_install_cursor() -> str | None:
    """Offer to install the missing ``cursor`` extra; return a status line.

    Shown atop the Cursor drill-in when the optional-extra ``cursor-sdk`` is
    absent. Three-choice ``select`` like :func:`_prompt_install_antigravity` /
    :func:`_prompt_install_harness` (install now / set key anyway / show
    command), but does NOT gate key management on the SDK: the ``cursor:`` key
    is stored independently and is useful once the SDK lands, so declining falls
    through to the key menu (whereas ``_prompt_install_harness`` returns to the
    picker, since pi can't configure credentials without its CLI). Install is
    portable and index-free — see
    :func:`omnigent.onboarding.cursor_auth.cursor_install_command`.

    :returns: Status string for the drill-in's transient status line, or
        ``None`` (set-key-anyway / Esc / printed-command, no actionable result).
    """
    from rich.markup import escape as _rich_escape

    from omnigent.onboarding.cursor_auth import CURSOR_EXTRA, install_cursor_sdk
    from omnigent.onboarding.extra_install import extra_install_display
    from omnigent.onboarding.interactive import console, select

    cmd = extra_install_display(CURSOR_EXTRA)
    # ``select`` renders text through Rich markup; escape the literal
    # ``[cursor]`` so it renders verbatim.
    cmd_markup = _rich_escape(cmd)
    choice = select(
        "Cursor's SDK (cursor-sdk) isn't installed. Install it now?",
        [
            f"Install it now ({cmd_markup})",
            "Set the Cursor key anyway",
            "I'll run it myself (show the command)",
        ],
        descriptions=[
            f"Runs `{cmd_markup}`, then continues.",
            "Skip the install — store the key now; the SDK can be added later.",
            "Print the command so you can install it yourself, then continue.",
        ],
        default=0,
        clear_on_exit=True,
    )
    if choice == 0:
        console.print(f"  [dim]Installing the cursor extra — running `{cmd_markup}`…[/dim]")
        if install_cursor_sdk():
            console.print("  [green]✓ cursor-sdk installed[/green]")
            return "✓ cursor-sdk installed"
        console.print(f"  [red]Install failed.[/red] Run it manually: [bold]{cmd_markup}[/bold]")
        return "✗ Install failed — set the key anyway, or install by hand"
    if choice < 0:
        return _SOFT_INSTALL_ABORT
    if choice == 2:  # run it yourself
        console.print(f"  Install the cursor extra with:\n    [bold]{cmd_markup}[/bold]")
        return None
    # choice == 1 (set key anyway): fall through to the key menu silently.
    return None


def _manage_cursor_sdk_harness() -> None:
    """Run the Cursor SDK loop: manage its ``CURSOR_API_KEY``.

    Cursor runs via the ``cursor-sdk`` package and authenticates against
    Cursor's own backend with a ``CURSOR_API_KEY`` — the SDK requires one (a
    ``cursor-agent login`` does not apply, and cursor has no provider/gateway
    family). So this manages exactly that credential: set / replace / remove an
    API key stored in the omnigent secret store, mirroring how the other
    harnesses persist their api keys (the secret in the store, a
    ``keychain:``/``env:`` reference in ``~/.omnigent/config.yaml``).

    When the optional ``cursor-sdk`` is missing, the drill-in first offers to
    install it (:func:`_prompt_install_cursor`). Unlike the CLI-backed harnesses
    (which gate on the CLI), declining still drops into the key menu — the
    ``cursor:`` key is independently storable. Mirrors Antigravity post-#322.

    :returns: None. Side effects: may install the ``cursor`` extra, and may
        write the ``cursor:`` block of ``~/.omnigent/config.yaml`` and the
        secret store.
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.cursor_auth import (
        cursor_api_key_configured,
        cursor_api_key_ref,
        cursor_sdk_installed,
    )
    from omnigent.onboarding.interactive import select

    # Offer the install once on entry (not per loop iteration) when the SDK is
    # absent; the result seeds the menu's status line. Declining falls through
    # to key management, since the key is SDK-independent.
    status: str | None = None
    if not cursor_sdk_installed():
        status = _prompt_install_cursor()
        if status == _SOFT_INSTALL_ABORT:
            return
    while True:
        config = _load_global_config()
        key_set = cursor_api_key_configured(config)

        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow(
                "Replace API key (CURSOR_API_KEY)" if key_set else "Set API key (CURSOR_API_KEY)",
                action="set_key",
            )
        ]
        if key_set:
            rows.append(_HarnessMenuRow("Remove API key", action="remove_key"))
        rows.append(_HarnessMenuRow("← Back", action="back"))

        header = "Cursor — API key configured" if key_set else "Cursor — no API key yet"
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "set_key":
            status = _set_cursor_api_key()
        elif action == "remove_key":
            ref = cursor_api_key_ref(config)
            # Only a keychain-stored secret is ours to delete; an ``env:`` ref
            # points at the user's own environment, so just drop the config.
            if ref is not None and ref.startswith("keychain:"):
                secret_store.delete_secret(ref[len("keychain:") :])
            _save_global_config({}, unset_keys=("cursor",))
            status = "✓ Removed Cursor API key"


def _set_cursor_api_key() -> str | None:
    """Prompt for and store a Cursor ``CURSOR_API_KEY``; return a status line.

    Offers an existing ``CURSOR_API_KEY`` from the environment first (recorded
    as an ``env:`` reference, so the secret never enters the config or the
    secret store), else reads the key with a hidden prompt and stores it in the
    omnigent secret store under ``keychain:cursor``. The ``crsr_`` prefix is
    validated with a soft warning so a wrong paste is caught without
    hard-blocking a future key format. The key value is never echoed.

    :returns: A confirmation string for the menu's transient status, or
        ``None`` when the user aborted (empty input / declined the warning).
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.cursor_auth import (
        CURSOR_SECRET_NAME,
        cursor_api_key_settings,
        looks_like_cursor_api_key,
    )
    from omnigent.onboarding.interactive import prompt_text

    # Strip surrounding whitespace before validating/forwarding so a key
    # exported with a trailing newline (a common ``export $(…)`` mishap)
    # validates and resolves cleanly — matching the pasted-key branch's
    # ``.strip()`` below and the strip in ``resolve_secret``'s ``env:`` branch.
    raw_detected = os.environ.get("CURSOR_API_KEY")
    detected = raw_detected.strip() if raw_detected else None
    if detected and click.confirm(
        "Detected CURSOR_API_KEY in the environment — use it?", default=True
    ):
        if not looks_like_cursor_api_key(detected) and not click.confirm(
            "$CURSOR_API_KEY doesn't start with 'crsr_'. Use it anyway?", default=False
        ):
            return None
        _save_global_config(cursor_api_key_settings("env:CURSOR_API_KEY"))
        return "✓ Cursor API key set (from $CURSOR_API_KEY)"

    pasted = prompt_text("Cursor API key (CURSOR_API_KEY)", hide_input=True).strip()
    if not pasted:
        return None
    if not looks_like_cursor_api_key(pasted) and not click.confirm(
        "That doesn't start with 'crsr_'. Store it anyway?", default=False
    ):
        return None
    secret_store.store_secret(CURSOR_SECRET_NAME, pasted)
    _save_global_config(cursor_api_key_settings(f"keychain:{CURSOR_SECRET_NAME}"))
    return "✓ Cursor API key stored"


def _manage_cursor_native_harness() -> None:
    """Configure the ``cursor-agent`` CLI used by the built-in web agent."""
    from omnigent.onboarding.harness_install import (
        CURSOR_KEY,
        harness_cli_installed,
        harness_cli_logged_in,
        harness_install_spec,
        harness_login,
        harness_logout,
    )
    from omnigent.onboarding.interactive import console, select

    if not harness_cli_installed(CURSOR_KEY):
        spec = harness_install_spec(CURSOR_KEY)
        hint = (
            spec.install_hint
            if spec and spec.install_hint
            else "curl https://cursor.com/install -fsS | bash"
        )
        console.print(
            "  Cursor CLI isn't installed. Install it with:\n"
            f"    [bold]{hint}[/bold]\n"
            "  then run [bold]cursor-agent login[/bold] or re-open this menu."
        )
        return

    status: str | None = None
    while True:
        logged_in = harness_cli_logged_in(CURSOR_KEY)
        header = "Cursor CLI — logged in" if logged_in else "Cursor CLI — not logged in yet"
        rows = [_HarnessMenuRow("Sign in (cursor-agent login)", action="login")]
        if logged_in:
            rows.append(_HarnessMenuRow("Sign out (cursor-agent logout)", action="logout"))
        rows.append(_HarnessMenuRow("← Back", action="back"))
        idx = select(header, [row.label for row in rows], clear_on_exit=True, status=status)
        if idx < 0 or rows[idx].action == "back":
            return
        if rows[idx].action == "login":
            status = (
                "✓ Cursor CLI logged in" if harness_login(CURSOR_KEY) else "Login not detected"
            )
        elif rows[idx].action == "logout":
            status = "✓ Cursor CLI logged out" if harness_logout(CURSOR_KEY) else "Logout failed"


def _manage_cursor_harness() -> None:
    """Configure Cursor CLI and SDK from one consolidated setup entry."""
    from omnigent.onboarding.cursor_auth import cursor_api_key_configured
    from omnigent.onboarding.harness_install import (
        CURSOR_KEY,
        harness_cli_installed,
        harness_cli_logged_in,
    )
    from omnigent.onboarding.interactive import select

    while True:
        cli_status = (
            "logged in"
            if harness_cli_logged_in(CURSOR_KEY)
            else "needs login"
            if harness_cli_installed(CURSOR_KEY)
            else "not installed"
        )
        sdk_status = "API key configured" if cursor_api_key_configured() else "not configured"
        rows = [
            _HarnessMenuRow(f"Cursor CLI — {cli_status}", action="cli"),
            _HarnessMenuRow(f"Cursor SDK — {sdk_status}", action="sdk"),
            _HarnessMenuRow("← Back", action="back"),
        ]
        idx = select("Cursor setup", [row.label for row in rows], clear_on_exit=True)
        if idx < 0 or rows[idx].action == "back":
            return
        if rows[idx].action == "cli":
            _manage_cursor_native_harness()
        elif rows[idx].action == "sdk":
            _manage_cursor_sdk_harness()


def _prompt_install_antigravity() -> str | None:
    """Offer to install the missing ``antigravity`` extra; return a status line.

    Shown atop the Antigravity drill-in when the ``google-antigravity`` SDK is absent.
    Mirrors :func:`_prompt_install_harness` — a three-choice ``select`` (install now /
    set key anyway / print command) — but does NOT gate key management on the SDK:
    unlike pi (which can't be configured without its CLI), the ``antigravity:`` key is
    storable independently, so declining just falls through to the key menu. The
    install carries no index URL (see :func:`antigravity_install_command`); on failure
    it prints the command to run by hand.

    :returns: A status string for the drill-in's transient status (install result or
        printed-command note), or ``None`` on set-key-anyway / Esc.
    """
    from rich.markup import escape as _rich_escape

    from omnigent.onboarding.antigravity_auth import ANTIGRAVITY_EXTRA, install_antigravity_sdk
    from omnigent.onboarding.extra_install import extra_install_display
    from omnigent.onboarding.interactive import console, select

    cmd = extra_install_display(ANTIGRAVITY_EXTRA)
    # ``select`` renders through Rich markup, so escape the literal ``[antigravity]``.
    cmd_markup = _rich_escape(cmd)
    choice = select(
        "Antigravity's SDK (google-antigravity) isn't installed. Install it now?",
        [
            f"Install it now ({cmd_markup})",
            "Set the Gemini key anyway",
            "I'll run it myself (show the command)",
        ],
        descriptions=[
            f"Runs `{cmd_markup}`, then continues.",
            "Skip the install — store the key now; the SDK can be added later.",
            "Print the command so you can install it yourself, then continue.",
        ],
        default=0,
        clear_on_exit=True,
    )
    if choice == 0:
        console.print(f"  [dim]Installing the antigravity extra — running `{cmd_markup}`…[/dim]")
        if install_antigravity_sdk():
            console.print("  [green]✓ google-antigravity installed[/green]")
            return "✓ google-antigravity installed"
        console.print(f"  [red]Install failed.[/red] Run it manually: [bold]{cmd_markup}[/bold]")
        return "✗ Install failed — set the key anyway, or install by hand"
    if choice < 0:
        return _SOFT_INSTALL_ABORT
    if choice == 2:
        console.print(f"  Install the antigravity extra with:\n    [bold]{cmd_markup}[/bold]")
        return None
    # choice == 1 (set key anyway): fall through to the key menu silently.
    return None


def _manage_antigravity_harness() -> None:
    """Run the level-2 loop for Antigravity: set / replace / remove its Gemini key.

    Antigravity is Gemini-native (no provider family), so this manages just its
    API key — stored in the secret store, referenced from the ``antigravity:``
    config block — mirroring how the other harnesses persist api keys.

    When the optional ``google-antigravity`` SDK is missing, the drill-in first offers
    to install it (:func:`_prompt_install_antigravity`). Unlike the CLI-backed harnesses
    (whose drill-in *gates* on the CLI), declining here still drops into the key menu,
    since the ``antigravity:`` key is independently storable.

    :returns: None. Side effects: may install the ``antigravity`` extra, and may write
        the ``antigravity:`` config block and the secret store.
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.antigravity_auth import (
        ANTIGRAVITY_CONFIG_KEY,
        ANTIGRAVITY_SECRET_NAME,
        antigravity_api_key_configured,
        antigravity_api_key_ref,
        antigravity_sdk_installed,
    )
    from omnigent.onboarding.interactive import select

    # Offer the install once on entry (not per loop iteration); the returned status
    # seeds the menu's transient status line.
    status: str | None = None
    if not antigravity_sdk_installed():
        status = _prompt_install_antigravity()
        if status == _SOFT_INSTALL_ABORT:
            return
    while True:
        config = _load_global_config()
        key_set = antigravity_api_key_configured(config)

        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow(
                "Replace Gemini API key" if key_set else "Set Gemini API key",
                action="set_key",
            )
        ]
        if key_set:
            rows.append(_HarnessMenuRow("Remove API key", action="remove_key"))
        rows.append(_HarnessMenuRow("← Back", action="back"))

        header = (
            "Antigravity — Gemini API key configured"
            if key_set
            else "Antigravity — no Gemini API key yet"
        )
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "set_key":
            status = _set_antigravity_api_key()
        elif action == "remove_key":
            ref = antigravity_api_key_ref(config)
            # Only the secret we own (``keychain:antigravity``) is ours to
            # delete: a hand-edited block may point at a shared ``keychain:<other>``
            # secret, and an ``env:`` ref names the user's own environment. In
            # both of those cases just drop the config block and leave the secret.
            if ref == f"keychain:{ANTIGRAVITY_SECRET_NAME}":
                secret_store.delete_secret(ANTIGRAVITY_SECRET_NAME)
            _save_global_config({}, unset_keys=(ANTIGRAVITY_CONFIG_KEY,))
            status = "✓ Removed Gemini API key"


def _set_antigravity_api_key() -> str | None:
    """Prompt for and store a Gemini API key; return a status line.

    Offers an existing ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY`` first
    (recorded as an ``env:`` ref, so the secret stays in the environment), else
    reads it with a hidden prompt and stores it under ``keychain:antigravity``.
    The key prefix (``AIza`` or ``AQ``) is checked softly (a wrong paste is
    caught but can be forced). The key is never echoed.

    :returns: A status string for the menu, or ``None`` if the user aborted.
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.antigravity_auth import (
        ANTIGRAVITY_API_KEY_PREFIX_HINT,
        ANTIGRAVITY_ENV_VARS,
        ANTIGRAVITY_SECRET_NAME,
        antigravity_api_key_settings,
        looks_like_gemini_api_key,
    )
    from omnigent.onboarding.interactive import prompt_text

    detected_var = next((v for v in ANTIGRAVITY_ENV_VARS if os.environ.get(v)), None)
    if detected_var is not None and click.confirm(
        f"Detected {detected_var} in the environment — use it?", default=True
    ):
        detected = os.environ[detected_var]
        if not looks_like_gemini_api_key(detected) and not click.confirm(
            f"${detected_var} doesn't start with {ANTIGRAVITY_API_KEY_PREFIX_HINT}. "
            "Use it anyway?",
            default=False,
        ):
            return None
        _save_global_config(antigravity_api_key_settings(f"env:{detected_var}"))
        return f"✓ Gemini API key set (from ${detected_var})"

    pasted = prompt_text("Gemini API key (GEMINI_API_KEY)", hide_input=True).strip()
    if not pasted:
        return None
    if not looks_like_gemini_api_key(pasted) and not click.confirm(
        f"That doesn't start with {ANTIGRAVITY_API_KEY_PREFIX_HINT}. Store it anyway?",
        default=False,
    ):
        return None
    secret_store.store_secret(ANTIGRAVITY_SECRET_NAME, pasted)
    _save_global_config(antigravity_api_key_settings(f"keychain:{ANTIGRAVITY_SECRET_NAME}"))
    return "✓ Gemini API key stored"


def _qwen_auth_configured() -> bool:
    """Best-effort check whether Qwen Code can authenticate non-interactively.

    Qwen has **no CLI login** — its ``auth`` subcommand was removed. For our
    ``qwen --acp`` executor, auth must come from one of:

    - API-key / provider env vars (the headless path): ``OPENAI_API_KEY``,
      ``BAILIAN_CODING_PLAN_API_KEY``, or ``OPENROUTER_API_KEY``; or
    - an auth type selected via the interactive ``/auth`` flow (API key or the
      Alibaba Cloud Coding Plan), persisted to ``~/.qwen/settings.json``.

    (Qwen OAuth was discontinued on 2026-04-15, so it is not an auth path here.)

    Best-effort: the env-var check is reliable; the on-disk check keys off
    ``settings.json`` fields whose schema is not contract-stable (see
    docs/QWEN_FOLLOWUPS.md). Returns ``False`` for a fresh install with no auth —
    the case that must NOT render as "signed in".

    :returns: ``True`` when auth is detectable, else ``False``.
    """
    from pathlib import Path

    if any(
        os.environ.get(v)
        for v in ("OPENAI_API_KEY", "BAILIAN_CODING_PLAN_API_KEY", "OPENROUTER_API_KEY")
    ):
        return True
    settings = Path.home() / ".qwen" / "settings.json"
    if settings.is_file():
        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            return False
        if isinstance(data, dict):
            if data.get("selectedAuthType"):
                return True
            security = data.get("security")
            auth = security.get("auth") if isinstance(security, dict) else None
            if isinstance(auth, dict) and (
                auth.get("selectedType") or auth.get("selectedAuthType")
            ):
                return True
    return False


def _print_qwen_auth_help() -> None:
    """Print Qwen's authentication options (it has no ``qwen login``)."""
    from omnigent.onboarding.interactive import console

    console.print(
        "\n  [bold]Authenticate Qwen Code[/bold]:\n"
        "    • Interactive: run [bold]qwen[/bold] and use [bold]/auth[/bold] "
        "(API key or Alibaba Cloud Coding Plan)\n"
        "    • Headless / ACP: set [bold]OPENAI_API_KEY[/bold] + "
        "[bold]OPENAI_BASE_URL[/bold] + [bold]OPENAI_MODEL[/bold]\n"
        "    • Coding Plan: [bold]BAILIAN_CODING_PLAN_API_KEY[/bold] + the "
        "Coding Plan base URL\n"
        "    • OpenRouter: [bold]OPENROUTER_API_KEY[/bold] + "
        "OPENAI_BASE_URL=https://openrouter.ai/api/v1\n"
    )


def _launch_qwen_auth() -> str | None:
    """Launch the interactive ``qwen`` TUI so the user can run ``/auth``.

    The ``/auth`` flow (API key or Alibaba Cloud Coding Plan) is interactive, so
    this hands the terminal to ``qwen``; when the user exits, re-check auth.

    :returns: A status line for the menu reflecting the post-launch auth state.
    """
    from omnigent.onboarding.harness_install import (
        QWEN_KEY,
        harness_cli_installed,
        harness_install_spec,
    )
    from omnigent.onboarding.interactive import console

    if not harness_cli_installed(QWEN_KEY):
        return "✗ qwen CLI not found"
    spec = harness_install_spec(QWEN_KEY)
    assert spec is not None
    console.print(
        "  [dim]Launching Qwen — type [bold]/auth[/bold] to configure authentication, "
        "then exit (/quit) to return.[/dim]"
    )
    with contextlib.suppress(OSError, KeyboardInterrupt):
        subprocess.run([spec.binary], check=False)
    return "✓ authentication detected" if _qwen_auth_configured() else "Auth not detected yet"


def _manage_qwen_harness() -> None:
    """Run the level-2 loop for Qwen Code: install the CLI and guide auth setup.

    Qwen has **no CLI subscription login** — its ``auth`` subcommand was removed.
    Authentication is either OpenAI-compatible env vars (for the headless
    ``qwen --acp`` path) or the interactive ``/auth`` command (API key or
    Alibaba Cloud Coding Plan). So this drill-in installs the CLI when missing,
    reports best-effort auth status (:func:`_qwen_auth_configured`), and offers
    to launch ``qwen`` for ``/auth`` — it does **not** pretend to run a ``qwen
    login``
    (there isn't one). Storing/injecting an OpenAI-compatible key *through
    Omnigent* is deferred (see docs/QWEN_FOLLOWUPS.md, Provider Injection).

    Like the CLI-backed harnesses, a missing CLI gates the drill-in — there's
    nothing to configure for a harness you can't run.

    :returns: None. Side effects: may ``npm install`` the qwen CLI and launch the
        interactive ``qwen`` TUI for ``/auth``.
    """
    from omnigent.onboarding.harness_install import (
        QWEN_KEY,
        harness_cli_installed,
        harness_install_command,
        install_harness_cli,
    )
    from omnigent.onboarding.interactive import console, select

    # Gate on the CLI. Offer to install it; declining (or copy-the-command)
    # returns to the harness picker.
    if not harness_cli_installed(QWEN_KEY):
        cmd = " ".join(harness_install_command(QWEN_KEY))
        choice = select(
            "Qwen Code's CLI isn't installed. Install it now?",
            [
                f"Yes — install ({cmd})",
                "No — back to harnesses",
                "I'll run it myself (show the command)",
            ],
            descriptions=[
                f"Runs `{cmd}` (needs npm), then continues to auth setup.",
                "Return to the harness picker without installing.",
                "Print the command so you can install it yourself, then return.",
            ],
            default=0,
            clear_on_exit=True,
        )
        if choice == 0:
            console.print(f"  [dim]Installing Qwen Code — running `{cmd}`…[/dim]")
            if install_harness_cli(QWEN_KEY):
                console.print("  [green]✓ Qwen Code installed[/green]")
            else:
                console.print(
                    f"  [red]Install failed.[/red] Run it manually, then re-open: "
                    f"[bold]{cmd}[/bold]"
                )
                return
        else:
            if choice == 2:  # run it yourself
                console.print(f"  Install Qwen Code with:\n    [bold]{cmd}[/bold]")
            return

    # Carry the prior action's confirmation as a transient status line.
    status: str | None = None
    while True:
        configured = _qwen_auth_configured()
        header = (
            "Qwen Code — authentication detected"
            if configured
            else "Qwen Code — not authenticated yet"
        )
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Open Qwen to run /auth", action="auth"),
            _HarnessMenuRow("Show auth options", action="help"),
            _HarnessMenuRow("← Back", action="back"),
        ]
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "auth":
            status = _launch_qwen_auth()
        elif action == "help":
            _print_qwen_auth_help()
            status = None


def _print_goose_auth_help() -> None:
    """Print Goose's configuration options (Omnigent manages no Goose credential)."""
    from omnigent.onboarding.interactive import console

    console.print(
        "\n  [bold]Configure Goose[/bold] (Omnigent stores no Goose credential):\n"
        "    • Interactive: run [bold]goose configure[/bold] to pick a provider "
        "and store its key (keyring or ~/.config/goose/config.yaml)\n"
        "    • Env override: set [bold]GOOSE_PROVIDER[/bold] + [bold]GOOSE_MODEL[/bold] "
        "(plus the provider's key, e.g. ANTHROPIC_API_KEY / OPENAI_API_KEY)\n"
    )


def _launch_goose_configure() -> str | None:
    """Launch the interactive ``goose configure`` flow; return a status line.

    ``goose configure`` is interactive (pick a provider, enter its key), so this
    hands the terminal to ``goose``; when the user exits, re-read the configured
    provider. Mirrors :func:`_launch_qwen_auth`.

    :returns: A status line reflecting the post-configure provider state.
    """
    from omnigent.onboarding.goose_auth import goose_config_summary
    from omnigent.onboarding.harness_install import (
        GOOSE_KEY,
        harness_cli_installed,
        harness_install_spec,
    )
    from omnigent.onboarding.interactive import console

    if not harness_cli_installed(GOOSE_KEY):
        return "✗ goose CLI not found"
    spec = harness_install_spec(GOOSE_KEY)
    assert spec is not None
    console.print(
        "  [dim]Launching [bold]goose configure[/bold] — pick a provider and "
        "enter its key, then return.[/dim]"
    )
    with contextlib.suppress(OSError, KeyboardInterrupt):
        subprocess.run([spec.binary, "configure"], check=False)
    summary = goose_config_summary()
    if summary.provider:
        model = f" ({summary.model})" if summary.model else ""
        return f"✓ provider configured: {summary.provider}{model}"
    return "Provider not detected yet"


def _manage_goose_harness() -> None:
    """Run the level-2 loop for Goose: ensure the CLI, then guide ``goose configure``.

    Goose owns its own auth (keyring / ``~/.config/goose/config.yaml``) — Omnigent
    stores no Goose credential — so, like the Qwen drill-in, this reports
    best-effort configuration status and offers to launch ``goose configure``; it
    does not store a key through Omnigent. A missing CLI gates the drill-in
    (nothing to configure for a harness you can't run); Goose ships out-of-band
    (brew / curl, no npm package), so we show its install hint rather than
    auto-installing. Serves both ``goose-native`` (TUI) and the headless
    ``goose`` (ACP) harness — both launch the same ``goose`` binary and read the
    same config.

    :returns: None. Side effects: may launch the interactive ``goose configure``.
    """
    from omnigent.onboarding.goose_auth import goose_config_summary
    from omnigent.onboarding.harness_install import (
        GOOSE_KEY,
        harness_cli_installed,
        harness_install_spec,
    )
    from omnigent.onboarding.interactive import console, select

    # Gate on the CLI. Goose installs out-of-band (no npm package), so we can't
    # auto-install — show the hint and return.
    if not harness_cli_installed(GOOSE_KEY):
        spec = harness_install_spec(GOOSE_KEY)
        hint = spec.install_hint if spec and spec.install_hint else "brew install block-goose-cli"
        console.print(
            f"  Goose's CLI isn't installed. Install it with:\n    [bold]{hint}[/bold]\n"
            "  then re-open this menu."
        )
        return

    status: str | None = None
    while True:
        summary = goose_config_summary()
        if summary.provider:
            model = f" · {summary.model}" if summary.model else ""
            header = f"Goose — provider configured: {summary.provider}{model}"
        else:
            header = "Goose — no provider configured yet"
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Run goose configure", action="configure"),
            _HarnessMenuRow("Show configuration options", action="help"),
            _HarnessMenuRow("← Back", action="back"),
        ]
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "configure":
            status = _launch_goose_configure()
        elif action == "help":
            _print_goose_auth_help()
            status = None


def _print_acp_examples() -> None:
    """Print example ACP-agent commands (Omnigent stores no credential)."""
    from omnigent.onboarding.interactive import console

    console.print(
        "\n  [bold]Custom ACP agents[/bold] — connect any agent that speaks the "
        "Agent Client Protocol ([underline]agentclientprotocol.com[/underline]).\n"
        "  Omnigent stores no credential — log into each agent via its own CLI first.\n\n"
        "  Example commands to paste:\n"
        "    • Gemini CLI     [bold]gemini --experimental-acp[/bold]\n"
        "    • Qwen Code      [bold]qwen --acp[/bold]\n"
        "    • Goose          [bold]goose acp[/bold]\n"
        "    • Claude Code    [bold]npx -y @zed-industries/claude-code-acp[/bold]\n"
    )


def _add_acp_agent() -> None:
    """Prompt for a new ACP agent and append it to the ``acp:`` config block.

    Reached straight from the "Add custom ACP agent" overview row (no
    intermediate menu). Prints the paste-ready examples first, then prompts for
    name / command / optional model.
    """
    from omnigent.onboarding.acp_auth import (
        AcpAgentEntry,
        acp_agents,
        acp_agents_settings,
        slugify,
    )
    from omnigent.onboarding.interactive import console, prompt_text

    _print_acp_examples()
    name = prompt_text("Agent name (e.g. Gemini CLI)").strip()
    if not name:
        console.print("  [yellow]No name entered — nothing added.[/yellow]")
        return
    command = prompt_text("Command to launch (e.g. gemini --experimental-acp)").strip()
    if not command:
        console.print("  [yellow]No command entered — nothing added.[/yellow]")
        return
    model = (prompt_text("Model (optional — Enter to skip)", default="") or "").strip() or None

    entries = list(acp_agents())
    entries.append(AcpAgentEntry(slug=slugify(name), name=name, command=command, model=model))
    _save_global_config(acp_agents_settings(entries))
    console.print(f"  ✓ Added {name}")


def _manage_acp_agent(slug: str) -> None:
    """Per-agent drill-in for one configured ACP agent: remove it.

    Reached by selecting the agent's own row in the configure-harnesses overview.
    A single-shot menu (Remove / Back) — Omnigent stores no credential, so there
    is nothing else to manage per agent yet.

    :param slug: The agent's slug (see :func:`omnigent.onboarding.acp_auth.slugify`).
    """
    from omnigent.onboarding.acp_auth import acp_agents, acp_agents_settings
    from omnigent.onboarding.interactive import console, select

    agents = list(acp_agents())
    agent = next((a for a in agents if a.slug == slug), None)
    if agent is None:
        return
    suffix = f"  ·  {agent.model}" if agent.model else ""
    header = f"{agent.name} — {agent.command}{suffix}"
    rows: list[_HarnessMenuRow] = [
        _HarnessMenuRow("Remove this agent", action="remove"),
        _HarnessMenuRow("← Back", action="back"),
    ]
    idx = select(header, [r.label for r in rows], clear_on_exit=True)
    if idx < 0 or rows[idx].action == "back":
        return
    _save_global_config(acp_agents_settings([a for a in agents if a.slug != slug]))
    console.print(f"  ✓ Removed {agent.name}")


def _manage_hermes_harness() -> None:
    """Run the level-2 loop for Hermes: install the CLI, then configure it.

    Hermes owns its own auth via ``hermes model`` (interactive provider/model
    picker) and is installed via a curl script from Nous Research — Omnigent
    stores no Hermes credential. A missing CLI offers to run the vendor
    installer; when installed, the drill-in offers to launch ``hermes model``
    for provider configuration.

    :returns: None. Side effects: may install Hermes or launch ``hermes model``.
    """
    from omnigent.onboarding.harness_install import (
        HERMES_KEY,
        harness_cli_installed,
        harness_install_spec,
        install_harness_cli,
    )
    from omnigent.onboarding.interactive import console, select

    if not harness_cli_installed(HERMES_KEY):
        spec = harness_install_spec(HERMES_KEY)
        hint = (
            spec.install_hint
            if spec and spec.install_hint
            else "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
        )
        choice = select(
            "Hermes isn't installed. Install it now?",
            [
                f"Yes — install ({hint})",
                "No — back to harnesses",
                "I'll run it myself (show the command)",
            ],
            descriptions=[
                f"Runs `{hint}`.",
                "Return to the harness picker without installing.",
                "Print the command so you can install it yourself, then return.",
            ],
            default=0,
            clear_on_exit=True,
        )
        if choice == 0:
            console.print(f"  [dim]Installing Hermes — running `{hint}`…[/dim]")
            if install_harness_cli(HERMES_KEY):
                console.print("  [green]✓ Hermes installed[/green]")
            else:
                console.print(
                    f"  [red]Install failed.[/red] Run it manually, then re-open: "
                    f"[bold]{hint}[/bold]"
                )
                return
        elif choice == 2:
            console.print(f"  Install Hermes with:\n    [bold]{hint}[/bold]")
            return
        else:
            return

    status: str | None = None
    while True:
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Run hermes model (configure provider)", action="model"),
            _HarnessMenuRow("← Back", action="back"),
        ]
        idx = select(
            "Hermes Agent",
            [r.label for r in rows],
            clear_on_exit=True,
            status=status,
        )
        if idx < 0:
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "model":
            import subprocess

            try:
                subprocess.run(["hermes", "model"], check=False)
                status = "✓ hermes model completed"
            except FileNotFoundError:
                status = "✗ hermes binary not found"


def _manage_kiro_harness() -> None:
    """Run the level-2 loop for Kiro: ensure the CLI is installed and signed in.

    Kiro owns its own auth via ``kiro-cli login`` (Builder ID / social login /
    Identity Center) and is installed via Kiro's curl installer — Omnigent stores
    no Kiro credential. A missing CLI gates the drill-in; when installed, the
    drill-in offers to launch ``kiro-cli login`` to sign in. Mirrors
    :func:`_manage_hermes_harness`.

    :returns: None. Side effects: may launch ``kiro-cli login``.
    """
    from omnigent.onboarding.harness_install import (
        KIRO_KEY,
        harness_cli_installed,
        harness_install_spec,
    )
    from omnigent.onboarding.interactive import console, select

    if not harness_cli_installed(KIRO_KEY):
        spec = harness_install_spec(KIRO_KEY)
        hint = (
            spec.install_hint
            if spec and spec.install_hint
            else "curl -fsSL https://cli.kiro.dev/install | bash"
        )
        console.print(
            f"  Kiro isn't installed. Install it with:\n    [bold]{hint}[/bold]\n"
            "  then re-open this menu."
        )
        return

    status: str | None = None
    while True:
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Run kiro-cli login (sign in)", action="login"),
            _HarnessMenuRow("← Back", action="back"),
        ]
        idx = select(
            "Kiro",
            [r.label for r in rows],
            clear_on_exit=True,
            status=status,
        )
        if idx < 0:
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "login":
            import subprocess

            try:
                subprocess.run(["kiro-cli", "login"], check=False)
                status = "✓ kiro-cli login completed"
            except FileNotFoundError:
                status = "✗ kiro-cli binary not found"


def _print_kimi_auth_help() -> None:
    """Print Kimi Code's authentication options.

    Kimi authenticates against Moonshot AI's backend rather than an Omnigent
    credential: ``kimi login`` (OAuth or a Moonshot API key) for the default
    provider, and ``kimi provider add`` to register any other provider (an
    OpenAI-compatible endpoint, a Databricks gateway, …) in
    ``~/.kimi/config.toml``. Omnigent has no per-spawn provider override for
    upstream kimi, so all of this lives in the kimi CLI's own config —
    Omnigent-side injection remains a deferred follow-up.
    """
    from omnigent.onboarding.interactive import console

    console.print(
        "\n  [bold]Authenticate Kimi Code[/bold] (kimi manages its own config in "
        "~/.kimi/config.toml):\n"
        "    • Default provider: run [bold]kimi login[/bold] "
        "(Moonshot OAuth, or paste a Moonshot API key)\n"
        "    • Other providers: run [bold]kimi provider add[/bold] "
        "(OpenAI-compatible endpoint, gateway, …), then pin that model id in "
        "the agent spec\n"
        "    • Omnigent stores no kimi credential and cannot thread one per "
        "spawn — configure it once in the kimi CLI\n"
    )


def _manage_kimi_harness() -> None:
    """Run the level-2 loop for Kimi Code: install the CLI and drive ``kimi login``.

    Unlike Qwen (which has no ``login`` subcommand), Kimi ships a real
    ``kimi login`` (Moonshot OAuth or API key) and ``kimi logout``, so this
    drill-in offers sign-in / sign-out directly. Kimi has no first-class
    "am I logged in?" probe (its install spec sets ``status_args=None``), so
    :func:`~omnigent.onboarding.harness_install.harness_cli_logged_in` always
    reports ``False`` for it — meaning ``harness_login`` runs ``kimi login``
    every time it is asked (the interactive flow lets the user cancel if
    already authenticated) and its boolean return is not a reliable success
    signal. We therefore treat login / logout as best-effort side effects and
    report that the flow finished rather than asserting an auth state.

    Like the other CLI-backed harnesses, a missing CLI gates the drill-in —
    there is nothing to configure for a harness you can't run.

    :returns: None. Side effects: may install the kimi CLI and run
        ``kimi login`` / ``kimi logout`` in the foreground.
    """
    from omnigent.onboarding.harness_install import (
        KIMI_KEY,
        harness_cli_installed,
        harness_install_spec,
        harness_login,
        harness_logout,
    )
    from omnigent.onboarding.interactive import console, select

    # Gate on the CLI. Kimi ships a single binary via a curl installer (not
    # npm), so there's no in-process auto-install — name the command and let
    # the user run it, then re-open. Mirrors how ``harness_setup_hint`` treats
    # the other curl-installed CLI (cursor-agent).
    if not harness_cli_installed(KIMI_KEY):
        spec = harness_install_spec(KIMI_KEY)
        hint = (spec.install_hint if spec else None) or "see Kimi Code docs"
        console.print(
            "  Kimi Code's CLI isn't installed. Install it with:\n"
            f"    [bold]{hint}[/bold]\n"
            "  then re-open this menu to sign in."
        )
        return

    # Carry the prior action's confirmation as a transient status line.
    status: str | None = None
    while True:
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Sign in (kimi login)", action="login"),
            _HarnessMenuRow("Sign out (kimi logout)", action="logout"),
            _HarnessMenuRow("Show auth options", action="help"),
            _HarnessMenuRow("← Back", action="back"),
        ]
        idx = select(
            "Kimi Code — authentication is managed by the kimi CLI",
            [r.label for r in rows],
            clear_on_exit=True,
            status=status,
        )
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "login":
            # ``kimi login`` runs in the foreground (OAuth / API-key prompt);
            # its boolean return is unreliable for kimi (no status probe), so
            # don't assert success — just confirm the flow finished.
            console.print("  [dim]Signing in to Kimi (its login will open)…[/dim]")
            harness_login(KIMI_KEY)
            status = "kimi login flow finished — kimi stores its own credentials"
        elif action == "logout":
            console.print("  [dim]Signing out of Kimi…[/dim]")
            harness_logout(KIMI_KEY)
            status = "kimi logout flow finished"
        elif action == "help":
            _print_kimi_auth_help()
            status = None


def _prompt_install_copilot() -> str | None:
    """Offer to install the missing ``copilot`` extra; return a status line.

    Shown atop the Copilot drill-in when the optional-extra ``github-copilot-sdk``
    is absent. Three-choice ``select`` like :func:`_prompt_install_cursor` /
    :func:`_prompt_install_antigravity` (install now / set token anyway / show
    command), and like them does NOT gate token management on the SDK: the
    ``copilot:`` token is stored independently and is useful once the SDK lands,
    so declining falls through to the token menu. Install is portable and
    index-free — see
    :func:`omnigent.onboarding.copilot_auth.copilot_install_command`.

    :returns: Status string for the drill-in's transient status line, or
        ``None`` (set-token-anyway / Esc / printed-command, no actionable result).
    """
    from rich.markup import escape as _rich_escape

    from omnigent.onboarding.copilot_auth import COPILOT_EXTRA, install_copilot_sdk
    from omnigent.onboarding.extra_install import extra_install_display
    from omnigent.onboarding.interactive import console, select

    cmd = extra_install_display(COPILOT_EXTRA)
    # ``select`` renders text through Rich markup; escape the literal
    # ``[copilot]`` so it renders verbatim.
    cmd_markup = _rich_escape(cmd)
    choice = select(
        "Copilot's SDK (github-copilot-sdk) isn't installed. Install it now?",
        [
            f"Install it now ({cmd_markup})",
            "Set the GitHub token anyway",
            "I'll run it myself (show the command)",
        ],
        descriptions=[
            f"Runs `{cmd_markup}`, then continues.",
            "Skip the install — store the token now; the SDK can be added later.",
            "Print the command so you can install it yourself, then continue.",
        ],
        default=0,
        clear_on_exit=True,
    )
    if choice == 0:
        console.print(f"  [dim]Installing the copilot extra — running `{cmd_markup}`…[/dim]")
        if install_copilot_sdk():
            console.print("  [green]✓ github-copilot-sdk installed[/green]")
            return "✓ github-copilot-sdk installed"
        console.print(f"  [red]Install failed.[/red] Run it manually: [bold]{cmd_markup}[/bold]")
        return "✗ Install failed — set the token anyway, or install by hand"
    if choice < 0:
        return _SOFT_INSTALL_ABORT
    if choice == 2:  # run it yourself
        console.print(f"  Install the copilot extra with:\n    [bold]{cmd_markup}[/bold]")
        return None
    # choice == 1 (set token anyway): fall through to the token menu silently.
    return None


def _manage_copilot_harness() -> None:
    """Run the level-2 loop for Copilot: manage its GitHub token.

    Copilot runs via the ``github-copilot-sdk`` package and authenticates against
    GitHub's Copilot backend with a GitHub token — the SDK requires one and it
    has no provider/gateway family. So this manages exactly that credential:
    set / replace / remove a token stored in the omnigent secret store, mirroring
    how cursor / antigravity persist theirs (the secret in the store, a
    ``keychain:``/``env:`` reference in ``~/.omnigent/config.yaml``).

    When the optional ``github-copilot-sdk`` is missing, the drill-in first
    offers to install it (:func:`_prompt_install_copilot`). Unlike the CLI-backed
    harnesses (which gate on the CLI), declining still drops into the token
    menu — the ``copilot:`` token is independently storable. Mirrors cursor /
    antigravity.

    :returns: None. Side effects: may install the ``copilot`` extra, and may
        write the ``copilot:`` block of ``~/.omnigent/config.yaml`` and the
        secret store.
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.copilot_auth import (
        COPILOT_CONFIG_KEY,
        COPILOT_SECRET_NAME,
        copilot_github_token_configured,
        copilot_github_token_ref,
        copilot_sdk_installed,
    )
    from omnigent.onboarding.interactive import select

    # Offer the install once on entry (not per loop iteration) when the SDK is
    # absent; the result seeds the menu's status line. Declining falls through
    # to token management, since the token is SDK-independent.
    status: str | None = None
    if not copilot_sdk_installed():
        status = _prompt_install_copilot()
        if status == _SOFT_INSTALL_ABORT:
            return
    while True:
        config = _load_global_config()
        token_set = copilot_github_token_configured(config)

        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow(
                "Replace GitHub token" if token_set else "Set GitHub token",
                action="set_key",
            )
        ]
        if token_set:
            rows.append(_HarnessMenuRow("Remove GitHub token", action="remove_key"))
        rows.append(_HarnessMenuRow("← Back", action="back"))

        header = (
            "Copilot — GitHub token configured" if token_set else "Copilot — no GitHub token yet"
        )
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "set_key":
            status = _set_copilot_github_token()
        elif action == "remove_key":
            ref = copilot_github_token_ref(config)
            # Only the secret we own (``keychain:copilot``) is ours to delete: a
            # hand-edited block may point at a shared ``keychain:<other>`` secret,
            # and an ``env:`` ref names the user's own environment. In both of
            # those cases just drop the config block and leave the secret.
            if ref == f"keychain:{COPILOT_SECRET_NAME}":
                secret_store.delete_secret(COPILOT_SECRET_NAME)
            _save_global_config({}, unset_keys=(COPILOT_CONFIG_KEY,))
            status = "✓ Removed Copilot GitHub token"


def _set_copilot_github_token() -> str | None:
    """Prompt for and store a Copilot GitHub token; return a status line.

    Offers an existing ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN``
    first (recorded as an ``env:`` ref, so the secret stays in the environment),
    else reads it with a hidden prompt and stores it under ``keychain:copilot``.
    The token shape is checked softly (a classic ``ghp_`` PAT — which Copilot
    rejects — or a wrong paste is flagged but can be forced). The token is never
    echoed.

    :returns: A status string for the menu, or ``None`` if the user aborted.
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.copilot_auth import (
        COPILOT_SECRET_NAME,
        COPILOT_TOKEN_ENV_VARS,
        copilot_github_token_settings,
        looks_like_github_copilot_token,
    )
    from omnigent.onboarding.interactive import prompt_text

    detected_var = next((v for v in COPILOT_TOKEN_ENV_VARS if os.environ.get(v)), None)
    if detected_var is not None and click.confirm(
        f"Detected {detected_var} in the environment — use it?", default=True
    ):
        detected = os.environ[detected_var]
        if not looks_like_github_copilot_token(detected) and not click.confirm(
            f"${detected_var} doesn't look like a Copilot-capable GitHub token "
            "(github_pat_/gho_). Use it anyway?",
            default=False,
        ):
            return None
        _save_global_config(copilot_github_token_settings(f"env:{detected_var}"))
        return f"✓ Copilot GitHub token set (from ${detected_var})"

    pasted = prompt_text("GitHub token with Copilot access", hide_input=True).strip()
    if not pasted:
        return None
    if not looks_like_github_copilot_token(pasted) and not click.confirm(
        "That doesn't look like a Copilot-capable GitHub token (github_pat_/gho_). "
        "Store it anyway?",
        default=False,
    ):
        return None
    secret_store.store_secret(COPILOT_SECRET_NAME, pasted)
    _save_global_config(copilot_github_token_settings(f"keychain:{COPILOT_SECRET_NAME}"))
    return "✓ Copilot GitHub token stored"


def _manage_credential(provider: str, family: str) -> str | None:
    """Run the level-3 loop for one credential: make default / remove.

    Opened by selecting a credential at level 2. Offers ``Make default`` (only
    when it is not already this harness's default), ``Remove``, and ``← Back``.
    Make-default / remove return to level 2 with a confirmation; ``← Back`` /
    Esc / ``q`` return with no change.

    :param provider: The provider id of the chosen credential, e.g. ``"openai"``.
    :param family: The harness surface in context, ``"anthropic"`` /
        ``"openai"`` / ``"pi"``.
    :returns: A confirmation string to show as a transient status at level 2,
        or ``None`` when nothing changed.
    """
    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.interactive import select
    from omnigent.onboarding.provider_config import (
        DATABRICKS_KIND,
        SUBSCRIPTION_KIND,
        load_providers,
        surface_default_provider,
    )

    config = _load_global_config()
    entry = load_providers(config).get(provider)
    if entry is None:
        return None
    label = _family_credential_label(config, family, provider, entry)
    rows: list[_HarnessMenuRow] = []
    # "Make default" is offered unless this credential is already the
    # surface's *effective* default (matching the ✓ on the level-2 row) —
    # for pi that covers the fallback-driven default too, where offering
    # "make default" would be a confusing no-op.
    default = surface_default_provider(config, family)
    if default is None or default.name != provider:
        rows.append(
            _HarnessMenuRow(
                f"Make default for {family_label(family)}", action="set_default", provider=provider
            )
        )
    rows.append(_HarnessMenuRow("Remove", action="remove", provider=provider))
    rows.append(_HarnessMenuRow("← Back", action="back"))

    idx = select(label, [r.label for r in rows], clear_on_exit=True)
    if idx < 0:  # Esc / q — back to the credential list, no change
        return None
    row = rows[idx]
    if row.action == "back":
        return None
    if row.action == "set_default":
        return _set_harness_default(provider, family)
    # A subscription's credential lives in the harness CLI's own auth file, not
    # our config — so removing it means signing out of that CLI (otherwise the
    # login persists and ambient detection re-adopts it on the next open).
    if entry.kind == SUBSCRIPTION_KIND:
        return _remove_subscription(provider, family)
    # A databricks provider was wired by `ucode configure`, which edits
    # harness configs outside ~/.omnigent/config.yaml — so removing it
    # also cleans those edits up (otherwise codex keeps routing through
    # the workspace gateway).
    if entry.kind == DATABRICKS_KIND:
        return _remove_databricks_provider(provider)
    return _remove_credential(provider)


def _remove_subscription(provider: str, family: str) -> str | None:
    """Sign out of the harness CLI and remove the subscription credential.

    Unlike a key/gateway provider (whose credential is ours to drop), a
    subscription is backed by the harness CLI's own login file
    (``~/.codex/auth.json`` / ``~/.claude/.credentials.json``). Deleting only
    our entry would leave that login in place — so it would still drive the
    standalone CLI, and ambient detection would re-adopt the subscription on the
    next ``configure`` open. So "remove" here runs the harness's own logout
    (``codex logout`` / ``claude auth logout``) and then drops our entry. Guarded
    by an explicit confirm (default No) because it signs the user out of the
    standalone CLI too. (To merely stop *using* a subscription while staying
    logged in, the user makes another provider the default instead.)

    :param provider: The subscription provider id, e.g. ``"codex-subscription"``.
    :param family: The harness family, ``"anthropic"`` (Claude) / ``"openai"``
        (Codex).
    :returns: A confirmation message for the level-2 status line, or ``None``
        when the user declined (nothing changed). Side effects: runs the
        harness logout command and writes ``~/.omnigent/config.yaml``.
    """
    from omnigent.onboarding.harness_install import harness_install_spec, harness_logout
    from omnigent.onboarding.interactive import select

    spec = harness_install_spec(family)
    disp = spec.display if spec is not None else family
    logout_cmd = (
        f"{spec.binary} {' '.join(spec.logout_args)}"
        if spec is not None and spec.logout_args is not None
        else "logout"
    )
    choice = select(
        f"Remove {disp} subscription?",
        [f"Yes — sign out of {disp} and remove", "No — keep it"],
        descriptions=[
            f"Runs `{logout_cmd}`, signing you out of the standalone {disp} CLI "
            "too, then removes it here.",
            f"Leave the subscription and your {disp} login untouched.",
        ],
        default=1,  # default to the non-destructive choice
        clear_on_exit=True,
    )
    if choice != 0:
        return None
    signed_out = harness_logout(family)
    # Drop our entry regardless — the user asked to remove it. If logout failed
    # we say so, since the standalone login may persist (and be re-detected).
    _remove_credential(provider)
    if signed_out:
        return f"✓ Signed out of {disp} and removed"
    return (
        f"✓ Removed {disp} subscription — note: `{logout_cmd}` did not complete, "
        f"so you may still be signed in to the {disp} CLI"
    )


def _remove_databricks_provider(provider: str) -> str:
    """Remove a databricks provider and clean up ucode's harness wiring.

    A ``kind: databricks`` provider was wired by running ``ucode configure``
    (the add flow), which writes harness configs *outside*
    ``~/.omnigent/config.yaml`` — most damagingly, for Codex < 0.134.0 it
    rewrites the user's real ``~/.codex/config.toml`` (top-level
    ``profile = "ucode"``) so even the bare ``codex`` CLI routes through the
    workspace gateway, and ``ucode revert`` does not undo that edit. Removing
    the provider therefore undoes that wiring as part of the removal — no
    extra confirm, matching how a key provider's ``Remove`` acts immediately.
    The cleanup only ever touches ucode-namespaced artifacts (the ``profile``
    selector only when it equals ``"ucode"``; see
    :mod:`omnigent.onboarding.ucode_cleanup`), so the user's own settings
    are never at risk. Removal applies to every harness the provider
    serves — a databricks entry routes both Claude and Codex.

    :param provider: The databricks provider id, e.g. ``"databricks"``.
    :returns: A confirmation message for the level-2 status line reporting
        the removal and what wiring was cleaned (nothing extra is appended
        when no ucode wiring existed). Side effects: may edit
        ``~/.codex/config.toml``, delete ucode sidecar files, run
        ``claude mcp remove``, and write ``~/.omnigent/config.yaml``.
    """
    from omnigent.errors import OmnigentError
    from omnigent.onboarding.ucode_cleanup import remove_ucode_wiring

    cleanup_note = ""
    try:
        removal = remove_ucode_wiring()
    except (OmnigentError, OSError) as exc:
        # The entry removal below still proceeds — the user asked for it —
        # but say exactly what was left behind instead of failing silently.
        cleanup_note = f" — ucode cleanup incomplete: {exc}"
    else:
        cleaned: list[str] = []
        if removal.codex_config_stripped:
            cleaned.append("cleaned ~/.codex/config.toml")
        if removal.removed_sidecars:
            cleaned.append(f"deleted {len(removal.removed_sidecars)} ucode sidecar file(s)")
        if removal.web_search_mcp_removed:
            cleaned.append("unregistered ucode's web_search MCP")
        if cleaned:
            cleanup_note = f" — {', '.join(cleaned)}"
    removed_msg = _remove_credential(provider) or f"✓ Removed {provider}"
    return f"{removed_msg}{cleanup_note}"


def _set_harness_default(provider: str, family: str) -> str | None:
    """Make *provider* the default for *family* and persist wholesale.

    :param provider: The provider name to default, e.g. ``"openrouter"``.
    :param family: The harness surface to scope the default to,
        ``"anthropic"``, ``"openai"``, or ``"pi"`` — leaving the other
        harnesses' defaults untouched.
    :returns: A confirmation message for the caller to show as a transient
        status, or ``None`` when there was nothing to do. Side effect:
        writes ``~/.omnigent/config.yaml``.
    """
    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.provider_config import load_providers, set_default_provider

    block = _load_global_config().get("providers")
    if not isinstance(block, dict):
        return None
    entry = load_providers({"providers": block}).get(provider)
    label = _credential_label(provider, entry) if entry is not None else provider
    _save_global_config({"providers": set_default_provider(block, provider, family)})
    return f"✓ {label} is now the {family_label(family)} default"


def _clear_detection_dismissal(name: str) -> None:
    """Drop *name* from the persisted ``dismissed_detections`` list, if present.

    Called when the user explicitly re-adds a previously Removed (and thus
    dismissed) ambient credential — e.g. picking the detected codex
    config.toml provider from the add menu — so the detection behaves like
    an ordinary one again.

    :param name: The detection name to un-dismiss, e.g. ``"codex-databricks"``.
    :returns: None. Side effect: writes ``~/.omnigent/config.yaml`` when the
        name was dismissed; no write otherwise.
    """
    from omnigent.onboarding.detected import (
        DISMISSED_DETECTIONS_KEY,
        dismissed_detection_names,
    )

    dismissed = dismissed_detection_names(_load_global_config())
    if name not in dismissed:
        return
    _save_global_config({DISMISSED_DETECTIONS_KEY: sorted(dismissed - {name})})


def _remove_credential(provider: str) -> str | None:
    """Remove the *provider* credential and persist wholesale.

    The stored secret (if any) is left in place — removing a credential does
    not assume its key is unwanted.

    :param provider: The provider id to remove, e.g. ``"openrouter"``.
    :returns: A confirmation message for the caller to show as a transient
        status, or ``None`` when there was nothing to remove. Side effect:
        writes ``~/.omnigent/config.yaml`` (and, when the removed entry is
        backed by a live ambient detection that cannot be signed out,
        records its name under ``dismissed_detections`` so the next
        configure open does not silently re-adopt it).
    """
    from omnigent.onboarding.ambient import detect_providers
    from omnigent.onboarding.detected import (
        DISMISSED_DETECTIONS_KEY,
        dismissed_detection_names,
    )
    from omnigent.onboarding.provider_config import load_providers

    config = _load_global_config()
    block = config.get("providers")
    if not isinstance(block, dict) or provider not in block:
        return None
    entry = load_providers({"providers": block}).get(provider)
    label = _credential_label(provider, entry) if entry is not None else provider
    remaining = {k: v for k, v in block.items() if k != provider}
    settings: dict[str, Any] = {"providers": remaining}  # type: ignore[explicit-any]  # yaml-boundary mapping
    # If a live ambient detection backs this entry, removing the entry alone
    # is a no-op: the next configure open re-detects and re-adopts it (the
    # "Remove doesn't remove" bug). Subscriptions are exempt — their removal
    # path signs out of the CLI instead, and a future re-login SHOULD
    # re-adopt. Everything else (env API key, codex config.toml provider,
    # local Ollama) gets a persisted dismissal that the add menu's detected
    # option clears on re-add.
    backing = next(
        (d for d in detect_providers() if d.name == provider and d.kind != "subscription"),
        None,
    )
    if backing is not None:
        settings[DISMISSED_DETECTIONS_KEY] = sorted(dismissed_detection_names(config) | {provider})
    _save_global_config(settings)  # wholesale replace per key
    if backing is not None:
        return f"✓ Removed {label} — it stays on your machine but won't be auto-configured again"
    return f"✓ Removed {label}"


def _launch_opencode_auth_login() -> str | None:
    """Launch interactive ``opencode auth login``; return a post-login status.

    ``opencode auth login`` is interactive (pick a provider, sign in), so this
    hands the terminal to ``opencode`` and re-reads the credential state on
    return. Mirrors :func:`_launch_goose_configure`.
    """
    from omnigent.onboarding.harness_install import (
        OPENCODE_KEY,
        harness_cli_installed,
        harness_install_spec,
    )
    from omnigent.onboarding.interactive import console
    from omnigent.onboarding.opencode_auth import opencode_auth_summary

    if not harness_cli_installed(OPENCODE_KEY):
        return "✗ opencode CLI not found"
    spec = harness_install_spec(OPENCODE_KEY)
    assert spec is not None
    console.print(
        "  [dim]Launching [bold]opencode auth login[/bold] — pick a provider and "
        "sign in, then return.[/dim]"
    )
    with contextlib.suppress(OSError, KeyboardInterrupt):
        subprocess.run([spec.binary, "auth", "login"], check=False)
    summary = opencode_auth_summary()
    if summary.has_provider:
        return f"✓ providers: {summary.describe()}"
    return "No provider detected yet"


def _run_opencode_auth_list() -> None:
    """Show ``opencode auth list`` (stored credentials + detected env providers)."""
    from omnigent.onboarding.harness_install import OPENCODE_KEY, harness_install_spec

    spec = harness_install_spec(OPENCODE_KEY)
    if spec is None:
        return
    with contextlib.suppress(OSError, KeyboardInterrupt):
        subprocess.run([spec.binary, "auth", "list"], check=False)


def _list_opencode_models() -> list[str]:
    """Return the ``provider/model`` ids OpenCode can launch (``opencode models``).

    Best-effort: an absent CLI or a failed/empty invocation yields ``[]`` (the
    caller then tells the user to sign a provider in first).
    """
    from omnigent.onboarding.harness_install import OPENCODE_KEY, harness_install_spec

    spec = harness_install_spec(OPENCODE_KEY)
    if spec is None:
        return []
    try:
        result = subprocess.run(
            [spec.binary, "models"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _set_opencode_default_model(current: str | None) -> str | None:
    """Pick OpenCode's default model and persist it as ``opencode_model``.

    The choice is what ``omni opencode`` launches on when no ``--model`` is
    given — written into the per-session ``opencode.json`` at spawn so the TUI
    starts on it instead of ``opencode/big-pickle``. Returns a status line for
    the drill-in, or ``None`` when cancelled.

    :param current: The currently-persisted default model, or ``None``.
    """
    from omnigent.onboarding.interactive import console, select
    from omnigent.onboarding.opencode_auth import reachable_provider_ids

    models = _list_opencode_models()
    if not models:
        return "✗ no models — sign in to a provider first (opencode auth login)"
    # `opencode models` can list hundreds of `provider/model` ids across every
    # provider on models.dev — too long for the picker (it overflows the
    # viewport and flickers). Narrow to the providers the user can actually
    # authenticate (stored auth.json + env keys); fall back to the full list
    # only if that filter would hide everything.
    reachable = reachable_provider_ids()
    if reachable:
        scoped = [m for m in models if m.split("/", 1)[0] in reachable]
        models = scoped or models
    options = list(models)
    clear_index = -1
    if current is not None:
        clear_index = len(options)
        options.append("Clear default (use OpenCode's own default)")
    default = models.index(current) if current in models else 0
    # Even filtered to reachable providers the list can exceed the screen, so
    # bound the picker to a scrolling viewport sized to the terminal (leaving
    # room for the title / status / footer / "N more" markers).
    rows = shutil.get_terminal_size(fallback=(80, 24)).lines
    idx = select(
        "Pick OpenCode's default model",
        options,
        default=default,
        clear_on_exit=True,
        status=f"current: {current}" if current else None,
        max_visible=max(5, rows - 8),
    )
    if idx < 0:
        return None
    if idx == clear_index:
        _save_global_config({}, unset_keys=("opencode_model",))
        console.print("  [green]✓ default model cleared[/green]")
        return "✓ default model cleared"
    chosen = models[idx]
    _save_global_config({"opencode_model": chosen})
    console.print(f"  [green]✓ default model set to[/green] [bold]{chosen}[/bold]")
    return f"✓ default model: {chosen}"


def _print_opencode_auth_help() -> None:
    """Explain where OpenCode's model credentials come from."""
    from omnigent.onboarding.interactive import console

    console.print(
        "  OpenCode resolves a model from the provider its agent uses:\n"
        "    • [bold]opencode auth login[/bold] — sign in to a provider (OpenAI, Anthropic, …);\n"
        "      stored in ~/.local/share/opencode/auth.json.\n"
        "    • Provider env vars (OPENAI_API_KEY / ANTHROPIC_API_KEY / …) are auto-detected.\n"
        "    • Databricks gateway: set an agent ``profile`` (configured under Claude / Codex);\n"
        "      Omnigent synthesizes opencode's per-session provider config from it.\n"
        "  Omnigent stores no OpenCode credential of its own.\n"
        "  [dim]Tip:[/dim] 'Set default model' picks which model `omni opencode` launches on\n"
        "  (otherwise OpenCode uses its built-in default, opencode/big-pickle)."
    )


def _manage_opencode_harness() -> None:
    """Run the level-2 drill-in for OpenCode: ensure the CLI, then manage providers.

    OpenCode owns its own provider auth — ``opencode auth login`` (stored in
    ``~/.local/share/opencode/auth.json``) or ambient provider env vars — so,
    like the Goose / Qwen drill-ins, this reports which providers OpenCode can
    reach and offers to launch its native login; it never stores a key through
    Omnigent. (For the Databricks-gateway path the agent's ``profile`` is
    synthesized into opencode's per-session config instead — set under
    Claude / Codex.)

    OpenCode is npm-installable, so a missing CLI gates the drill-in with an
    install offer.

    :returns: None. Side effect: may ``npm install`` the opencode CLI.
    """
    from omnigent.onboarding.harness_install import (
        OPENCODE_KEY,
        harness_cli_installed,
        harness_install_command,
        install_harness_cli,
    )
    from omnigent.onboarding.interactive import console, select

    if not harness_cli_installed(OPENCODE_KEY):
        cmd = " ".join(harness_install_command(OPENCODE_KEY))
        choice = select(
            "OpenCode's CLI isn't installed. Install it now?",
            [
                f"Yes — install ({cmd})",
                "No — back to harnesses",
                "I'll run it myself (show the command)",
            ],
            descriptions=[
                f"Runs `{cmd}` (needs npm).",
                "Return to the harness picker without installing.",
                "Print the command so you can install it yourself, then return.",
            ],
            default=0,
            clear_on_exit=True,
        )
        if choice == 0:
            console.print(f"  [dim]Installing OpenCode — running `{cmd}`…[/dim]")
            if install_harness_cli(OPENCODE_KEY):
                console.print("  [green]✓ OpenCode installed[/green]")
            else:
                console.print(
                    f"  [red]Install failed.[/red] Run it manually, then re-open: "
                    f"[bold]{cmd}[/bold]"
                )
                return
        elif choice == 2:  # run it yourself
            console.print(f"  Install OpenCode with:\n    [bold]{cmd}[/bold]")
            return
        else:
            return

    # OpenCode owns its provider auth (``opencode auth login`` → auth.json) or
    # ambient env keys; Omnigent stores nothing. Report what's reachable and
    # offer to run its native login — like the Goose/Qwen drill-ins.
    status: str | None = None
    while True:
        from omnigent.onboarding.opencode_auth import opencode_auth_summary

        summary = opencode_auth_summary()
        default_model = _load_effective_config().get("opencode_model")
        header = (
            f"OpenCode — providers: {summary.describe()}"
            if summary.has_provider
            else "OpenCode — no provider configured yet"
        )
        model_label = (
            f"Set default model (current: {default_model})"
            if default_model
            else "Set default model"
        )
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Run opencode auth login", action="login"),
            _HarnessMenuRow(model_label, action="model"),
            _HarnessMenuRow("List providers & credentials", action="list"),
            _HarnessMenuRow("Show provider options", action="help"),
            _HarnessMenuRow("← Back", action="back"),
        ]
        idx = select(header, [r.label for r in rows], clear_on_exit=True, status=status)
        if idx < 0:  # Esc / q
            return
        action = rows[idx].action
        if action == "back":
            return
        if action == "login":
            status = _launch_opencode_auth_login()
        elif action == "model":
            status = _set_opencode_default_model(default_model)
        elif action == "list":
            _run_opencode_auth_list()
            status = None
        elif action == "help":
            _print_opencode_auth_help()
            status = None


def _run_configure_harnesses_interactive() -> None:
    """Run the interactive model/credential three-level picker.

    Invoked by ``omnigent setup --no-internal-beta`` and the bare-``run``
    first-run path, so both drive the identical flow.
    Opening it backfills a legacy databricks ``auth:`` block into a real
    provider and adopts any ambient-detected credential — announcing the
    newly auto-configured machine credentials in a callout — then loops on
    the level-1 harness overview. Every harness is shown on a single compact
    row — the harness name on the left, then an aligned ``✓``/``✗`` status
    column (the configured credential, or "Not installed" / "Not configured")
    — in 0.3 priority order: Claude, Codex, Cursor, OpenCode,
    Hermes, Pi, then Antigravity, Qwen Code, Goose, Copilot, Kiro, Kimi Code.
    The actionable hint (install command / next step) renders only for the
    highlighted row, as the selector's description line, so the overview stays
    uncluttered.

    :returns: None. Side effect: may write ``~/.omnigent/config.yaml`` via
        the backfill/adopt steps and any add/set-default/remove the user
        performs while navigating.
    """
    from rich.cells import cell_len
    from rich.markup import escape

    from omnigent.onboarding.antigravity_auth import (
        ANTIGRAVITY_ENV_VARS,
        ANTIGRAVITY_EXTRA,
        antigravity_api_key_configured,
        antigravity_sdk_installed,
    )
    from omnigent.onboarding.configure_models import family_label
    from omnigent.onboarding.copilot_auth import (
        COPILOT_EXTRA,
        COPILOT_TOKEN_ENV_VARS,
        copilot_github_token_configured,
        copilot_sdk_installed,
    )
    from omnigent.onboarding.cursor_auth import cursor_api_key_configured
    from omnigent.onboarding.extra_install import extra_install_display
    from omnigent.onboarding.goose_auth import goose_config_summary
    from omnigent.onboarding.harness_install import (
        COPILOT_KEY,
        CURSOR_KEY,
        GOOSE_KEY,
        HERMES_KEY,
        KIMI_KEY,
        KIRO_KEY,
        OPENCODE_KEY,
        QWEN_KEY,
        harness_cli_installed,
        harness_cli_logged_in,
        harness_install_command,
        harness_install_spec,
    )
    from omnigent.onboarding.interactive import select
    from omnigent.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        OPENAI_FAMILY,
        PI_SURFACE,
        surface_default_provider,
    )

    # Surface missing external tooling (Node ≥22.10 / tmux) the harnesses need,
    # once up front, so configuring a credential doesn't lead to a cryptic
    # failure when the harness later can't launch.
    _warn_missing_harness_dependencies()

    # Backfill a databricks provider from a legacy global auth: block FIRST (it
    # outranks ambient detection in routing), then adopt ambient detections.
    # The databricks backfill is silent (it just shows up in the harness status
    # line); newly-adopted machine credentials get a one-time callout naming
    # what was auto-configured and from where. No progress spinner here: a
    # transient spinner over the (fast) detection left a cleared-region gap and
    # a residual line directly above the menu on first paint.
    _adopt_ambient_credentials()

    # Level 1: pick a harness. The cursor moves between Claude, Codex, Pi, and
    # Quit; each harness's status renders as a non-selectable sub-line beneath
    # it (skipped by ↑/↓). Drilling in (level 2) keeps add/manage off this
    # overview. The menu clears in place on each choice so the session stays on
    # one screen. Quit / Esc / q exits.
    _QUIT = "\x00quit"  # sentinel marking the Quit row (not a family)
    # Sentinel marking the Antigravity row — it is not a provider family (Gemini
    # is outside the anthropic/openai machinery), so it dispatches to its own
    # credential manager rather than ``_manage_harness_providers``.
    _ANTIGRAVITY = "\x00antigravity"
    # Sentinel marking the Qwen Code row — like Antigravity/Cursor it is not a
    # provider family (its v1 auth is the CLI's own env vars / ``/auth`` flow,
    # not an Omnigent credential), so it dispatches to its own drill-in.
    _QWEN = "\x00qwen"
    # Sentinel marking the OpenCode row — native-server harness with no Omnigent
    # credential of its own (it routes through the bound agent's Databricks
    # gateway profile or ambient provider env), so it dispatches to its own
    # binary-install/info drill-in.
    _OPENCODE = "\x00opencode"
    # Sentinel marking the Goose row — like Qwen/Antigravity/Cursor it is not a
    # provider family (Goose owns its own auth via ``goose configure``, not an
    # Omnigent credential), so it dispatches to its own drill-in.
    _GOOSE = "\x00goose"
    # Sentinel marking the Hermes row — like Goose it owns its own auth via
    # ``hermes model`` and is installed via a curl installer.
    _HERMES = "\x00hermes"
    # Sentinel marking the Kiro row — like Goose/Hermes it owns its own auth (via
    # ``kiro-cli login``) and is installed via Kiro's curl installer, so it
    # dispatches to its own drill-in rather than a provider family.
    _KIRO = "\x00kiro"
    # Sentinel marking the Kimi Code row — like Cursor/Antigravity/Qwen it is
    # not a provider family. Auth lives entirely in the kimi CLI (``kimi login``
    # / ``kimi provider add`` → ~/.kimi/config.toml), so it dispatches to its
    # own drill-in rather than ``_manage_harness_providers``.
    _KIMI = "\x00kimi"
    # Sentinels for the generic-ACP rows. Each configured agent gets its own row
    # (``_ACP_AGENT_PREFIX + slug`` → per-agent remove drill-in); a single
    # ``_ACP_ADD`` row jumps straight into the add flow. Not a provider family —
    # each ACP agent owns its own auth.
    _ACP_ADD = "\x00acp-add"
    _ACP_AGENT_PREFIX = "\x00acp-agent:"
    families = [ANTHROPIC_FAMILY, OPENAI_FAMILY, PI_SURFACE]

    # Status glyph + Rich color per readiness kind: "ready" is a configured,
    # launchable harness (green ✓); "missing" is an absent CLI/SDK (red ✗);
    # "warn" is installed-but-unconfigured (yellow ✗ — present, not usable
    # yet); "action" is a do-something row (e.g. Add) with no status glyph. The
    # glyph leads the status, which sits in a left-aligned column right of the
    # names, so every ✓/✗ lines up in a single column.
    status_styles = {
        "ready": ("✓", "green"),
        "missing": ("✗", "red"),
        "warn": ("✗", "yellow"),
        "action": ("", "cyan"),
    }

    def _install_hint(command: str) -> str:
        # Selection-only tooltip. The command is escaped so a bracketed extra
        # (e.g. ``pip install "omnigent[cursor]"``) renders literally instead of
        # parsing as Rich markup.
        return f"Install with `{escape(command)}`"

    def _truncate_cells(text: str, max_cells: int) -> str:
        """Truncate *text* to a terminal-cell budget, adding an ellipsis if needed."""
        if cell_len(text) <= max_cells:
            return text
        ellipsis = "…"
        budget = max(0, max_cells - cell_len(ellipsis))
        out: list[str] = []
        used = 0
        for ch in text:
            width = cell_len(ch)
            if used + width > budget:
                break
            out.append(ch)
            used += width
        return "".join(out) + ellipsis

    def _family_row(fam: str) -> tuple[str, str, str, str, str]:
        # Claude / Codex / Pi: a CLI binary plus a usable default credential.
        # Pi's default is its *effective* one (explicit pi scope, else the
        # cross-family fallback).
        name = family_label(fam)
        if not harness_cli_installed(fam):
            return (
                fam,
                name,
                "Not installed",
                "missing",
                _install_hint(" ".join(harness_install_command(fam))),
            )
        default = surface_default_provider(config, fam)
        if default is None:
            return (fam, name, "Not configured", "warn", "Open to add a credential.")
        label = _family_credential_label(config, fam, default.name, default)
        return (fam, name, label, "ready", "")

    def build_harness_rows() -> list[tuple[str, str, str, str, str]]:
        # One visible row per harness, in 0.3 priority order. No folding — every
        # harness shows at once. Each row is (target, name, status, kind, hint),
        # where ``hint`` is the selection-only description (install command /
        # next step), empty for a ready harness.
        from omnigent.onboarding.hermes_auth import hermes_config_summary
        from omnigent.onboarding.opencode_auth import opencode_auth_summary

        rows: list[tuple[str, str, str, str, str]] = []
        rows.append(_family_row(ANTHROPIC_FAMILY))
        rows.append(_family_row(OPENAI_FAMILY))

        # Cursor setup covers both surfaces, but readiness prioritizes the CLI
        # used by the built-in web agent. An SDK key never hides a CLI problem.
        cursor_sdk_ready = cursor_api_key_configured(config) or bool(
            os.environ.get("CURSOR_API_KEY")
        )
        if not harness_cli_installed(CURSOR_KEY):
            cursor_spec = harness_install_spec(CURSOR_KEY)
            cursor_hint = (
                cursor_spec.install_hint
                if cursor_spec and cursor_spec.install_hint
                else "curl https://cursor.com/install -fsS | bash"
            )
            rows.append(
                (
                    CURSOR_KEY,
                    "Cursor",
                    "CLI not installed · SDK ready" if cursor_sdk_ready else "CLI not installed",
                    "missing",
                    _install_hint(cursor_hint),
                ),
            )
        elif harness_cli_logged_in(CURSOR_KEY):
            rows.append(
                (
                    CURSOR_KEY,
                    "Cursor",
                    "CLI + SDK ready" if cursor_sdk_ready else "CLI ready",
                    "ready",
                    "",
                )
            )
        else:
            rows.append(
                (
                    CURSOR_KEY,
                    "Cursor",
                    "CLI needs login · SDK ready" if cursor_sdk_ready else "CLI needs login",
                    "warn",
                    "Open to run `cursor-agent login`.",
                ),
            )

        # OpenCode — its own provider auth (login or env keys); the status is
        # what it can reach (e.g. "1 stored").
        opencode = opencode_auth_summary()
        if not opencode.installed:
            rows.append(
                (
                    _OPENCODE,
                    "OpenCode",
                    "Not installed",
                    "missing",
                    _install_hint(" ".join(harness_install_command(OPENCODE_KEY))),
                ),
            )
        elif opencode.ready:
            rows.append((_OPENCODE, "OpenCode", opencode.describe(), "ready", ""))
        else:
            rows.append(
                (
                    _OPENCODE,
                    "OpenCode",
                    "Not configured",
                    "warn",
                    "Open to sign in (opencode auth login).",
                ),
            )

        # Hermes — curl-installed; its provider/model live in
        # ``~/.hermes/config.yaml`` (written by `hermes model`). Read that so a
        # configured Hermes shows the picked model as ready, instead of always
        # reading "not configured" on an installed binary. A fresh install
        # ships ``provider: auto`` (nothing picked), so it still reads
        # "not configured" until `hermes model` selects a concrete provider.
        hermes = hermes_config_summary()
        if not hermes.installed:
            hermes_spec = harness_install_spec(HERMES_KEY)
            hermes_hint = (
                hermes_spec.install_hint
                if hermes_spec and hermes_spec.install_hint
                else "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
            )
            rows.append(
                (_HERMES, "Hermes", "Not installed", "missing", _install_hint(hermes_hint)),
            )
        elif hermes.ready:
            rows.append((_HERMES, "Hermes", hermes.describe(), "ready", ""))
        else:
            rows.append(
                (
                    _HERMES,
                    "Hermes",
                    "Not configured",
                    "warn",
                    "Open to configure with `hermes model`.",
                ),
            )

        rows.append(_family_row(PI_SURFACE))

        # Antigravity — Gemini key (antigravity-sdk extra is soft, like Cursor).
        if antigravity_api_key_configured(config) or any(
            os.environ.get(v) for v in ANTIGRAVITY_ENV_VARS
        ):
            rows.append((_ANTIGRAVITY, "Antigravity", "Gemini API key", "ready", ""))
        elif not antigravity_sdk_installed():
            rows.append(
                (
                    _ANTIGRAVITY,
                    "Antigravity",
                    "Not installed",
                    "missing",
                    _install_hint(extra_install_display(ANTIGRAVITY_EXTRA)),
                ),
            )
        else:
            rows.append(
                (
                    _ANTIGRAVITY,
                    "Antigravity",
                    "Not configured",
                    "warn",
                    "Open to add the Gemini API key.",
                ),
            )

        # Qwen Code — no CLI login; auth via OpenAI-compatible env vars or the
        # interactive /auth flow.
        if not harness_cli_installed(QWEN_KEY):
            rows.append(
                (
                    _QWEN,
                    "Qwen Code",
                    "Not installed",
                    "missing",
                    _install_hint(" ".join(harness_install_command(QWEN_KEY))),
                ),
            )
        elif _qwen_auth_configured():
            rows.append((_QWEN, "Qwen Code", "Authenticated", "ready", ""))
        else:
            rows.append(
                (
                    _QWEN,
                    "Qwen Code",
                    "Not configured",
                    "warn",
                    "Open to set up auth (/auth or env vars).",
                ),
            )

        # Goose — its own provider config via `goose configure`.
        if not harness_cli_installed(GOOSE_KEY):
            goose_spec = harness_install_spec(GOOSE_KEY)
            goose_hint = (
                goose_spec.install_hint
                if goose_spec and goose_spec.install_hint
                else "brew install block-goose-cli"
            )
            rows.append((_GOOSE, "Goose", "Not installed", "missing", _install_hint(goose_hint)))
        else:
            goose_summary = goose_config_summary()
            if goose_summary.provider:
                rows.append((_GOOSE, "Goose", goose_summary.provider, "ready", ""))
            else:
                rows.append(
                    (_GOOSE, "Goose", "Not configured", "warn", "Open to run `goose configure`."),
                )

        # Copilot — GitHub token (github-copilot-sdk extra is soft).
        if copilot_github_token_configured(config) or any(
            os.environ.get(v) for v in COPILOT_TOKEN_ENV_VARS
        ):
            rows.append((COPILOT_KEY, "Copilot", "GitHub token", "ready", ""))
        elif not copilot_sdk_installed():
            rows.append(
                (
                    COPILOT_KEY,
                    "Copilot",
                    "Not installed",
                    "missing",
                    _install_hint(extra_install_display(COPILOT_EXTRA)),
                ),
            )
        else:
            rows.append(
                (
                    COPILOT_KEY,
                    "Copilot",
                    "Not configured",
                    "warn",
                    "Open to add the GitHub token.",
                ),
            )

        # Kiro — native CLI, own auth via `kiro-cli login`; there is no
        # reliable local status probe, so an installed binary is still only
        # "not configured" until the user signs in.
        if harness_cli_installed(KIRO_KEY):
            rows.append(
                (_KIRO, "Kiro", "Not configured", "warn", "Sign in with `kiro-cli login`.")
            )
        else:
            kiro_spec = harness_install_spec(KIRO_KEY)
            kiro_hint = (
                kiro_spec.install_hint
                if kiro_spec and kiro_spec.install_hint
                else "curl -fsSL https://cli.kiro.dev/install | bash"
            )
            rows.append((_KIRO, "Kiro", "Not installed", "missing", _install_hint(kiro_hint)))

        # Kimi Code — native CLI, own auth via `kimi login`; there is no local
        # login status probe yet. Curl-installed (no npm package), so use its
        # install_hint when absent and show "not configured" when present.
        if harness_cli_installed(KIMI_KEY):
            rows.append(
                (_KIMI, "Kimi Code", "Not configured", "warn", "Sign in with `kimi login`.")
            )
        else:
            kimi_spec = harness_install_spec(KIMI_KEY)
            kimi_hint = (kimi_spec.install_hint if kimi_spec else None) or "see Kimi Code docs"
            rows.append((_KIMI, "Kimi Code", "Not installed", "missing", _install_hint(kimi_hint)))

        # Custom ACP agents — the generic `acp` harness driving any user-configured
        # ACP-agent command. Each configured agent gets its own overview row
        # (select → per-agent remove drill-in) so it sits alongside the built-in
        # harnesses, followed by an "Add" row that jumps straight into the add
        # flow. Not gated on a binary — each agent owns its own install.
        from omnigent.onboarding.acp_auth import acp_config_summary

        acp_summary = acp_config_summary()
        for agent in acp_summary.agents:
            rows.append(
                (
                    _ACP_AGENT_PREFIX + agent.slug,
                    agent.name,
                    f"ACP · {agent.command}",
                    "ready",
                    "Select to remove this ACP agent.",
                )
            )
        rows.append(
            (
                _ACP_ADD,
                "Add custom ACP agent" if acp_summary.configured else "Custom ACP agent",
                "" if acp_summary.configured else "None configured",
                "action",
                "Add an ACP agent (gemini, qwen, goose, …).",
            )
        )
        return rows

    while True:
        config = _load_global_config()
        harness_rows = build_harness_rows()
        # Place the status in a single column a fixed gutter right of the names,
        # so every ✓/✗ glyph lines up vertically (the earlier right-aligned
        # status scattered the glyphs and read as messy). The name column is the
        # widest harness name + a 4-space gutter; the status is escaped when
        # interpolated into markup so a credential label containing a ``[`` can't
        # parse as a Rich tag (descriptions are escaped the same way).
        name_col = max(len(name) for _t, name, *_rest in harness_rows) + 4
        term_width = max(40, shutil.get_terminal_size(fallback=(80, 24)).columns)
        # _render_menu prefixes selected rows with ``"    ❯  "`` (7 cells).
        # Cap the status text from the actual terminal width so verbose status
        # rows (e.g. OpenCode's provider summary) do not wrap in the compact
        # single-line overview.
        max_status_width = max(8, min(30, term_width - 7 - name_col - len("✓ ")))
        options: list[str] = []
        selectable: list[bool] = []
        row_target: list[str | None] = []
        descriptions: list[str] = []
        for target, name, status_text, kind, desc in harness_rows:
            status_text = _truncate_cells(status_text, max_status_width)
            glyph, color = status_styles[kind]
            options.append(f"{name.ljust(name_col)}[{color}]{glyph} {escape(status_text)}[/]")
            selectable.append(True)
            row_target.append(target)
            descriptions.append(desc)
        options.append("Quit")
        selectable.append(True)
        row_target.append(_QUIT)
        descriptions.append("")
        idx = select(
            "Configure harnesses",
            options,
            descriptions=descriptions,
            selectable=selectable,
            clear_on_exit=True,
            compact=True,
        )
        if idx < 0:  # Esc / q — exit
            return
        target = row_target[idx]
        if target == CURSOR_KEY:
            _manage_cursor_harness()
        elif target == COPILOT_KEY:
            _manage_copilot_harness()
        elif target in families:
            _manage_harness_providers(target)
        elif target == _ANTIGRAVITY:
            _manage_antigravity_harness()
        elif target == _QWEN:
            _manage_qwen_harness()
        elif target == _OPENCODE:
            _manage_opencode_harness()
        elif target == _GOOSE:
            _manage_goose_harness()
        elif target == _ACP_ADD:
            _add_acp_agent()
        elif isinstance(target, str) and target.startswith(_ACP_AGENT_PREFIX):
            _manage_acp_agent(target[len(_ACP_AGENT_PREFIX) :])
        elif target == _HERMES:
            _manage_hermes_harness()
        elif target == _KIRO:
            _manage_kiro_harness()
        elif target == _KIMI:
            _manage_kimi_harness()
        else:  # Quit row (or, defensively, a non-family row)
            return
