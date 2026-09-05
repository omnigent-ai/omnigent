"""Live model-flow CUJs (model-flows-design.md §10.1, the "UI-live" tier).

Every test drives the product the way a person uses it — the browser drives the
rig server's real SPA (or, for the rows whose actor is a REPL, a routing pin,
or an API client, the same REST calls), and harness truth is read from the
tmux pane — with REST snapshots as secondary probes only. Assertions encode the DESIGN's target
behavior, so this suite is red on unmodified main (and partially red on the PR
branch) in exactly the ways `model-flows-report.md` cites, and goes green as
the landing-order steps land. Run one row's red twin with::

    OMNIGENT_E2E_MODEL_FLOWS=1 OMNIGENT_E2E_MODEL_FLOWS_REPO=~/omnigent \\
        pytest tests/e2e/omnigent/test_model_flows_live.py -k row1 -x

Rows are numbered after the design's §10.1 table.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.claude_native import claude_catalog_fingerprint
from omnigent.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME, CODEX_NATIVE_AGENT_NAME
from tests.e2e.omnigent._model_flows_rig import (
    ModelFlowsRig,
    PaneWatcher,
    Ui,
    assistant_message_count,
    booted_rig,
    browser_ui,
    bypass_args,
    codex_config_copy_model,
    dismiss_blocking_dialogs,
    host_model_options,
    kill_pane,
    require_clis,
    require_opt_in,
    rest_create_session,
    rest_patch_session,
    rest_post_user_message,
    session_snapshot,
    shape_providers,
)
from tests.e2e.routing._helpers import wait_for

pytestmark = [pytest.mark.live_model_flows, pytest.mark.posix_only]

#: A version-free family word is not enough — resolved names carry versions.
_VERSIONED = re.compile(r"\d")


@pytest.fixture(autouse=True, scope="module")
def _opt_in() -> None:
    require_opt_in()
    require_clis("tmux")


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ModelFlowsRig]:
    """One rig server for the module; hosts restart per shape."""
    with booted_rig(tmp_path_factory.mktemp("model_flows_rig")) as booted:
        yield booted


@pytest.fixture(scope="module")
def shaped_host(rig: ModelFlowsRig) -> Callable[[str], str]:
    """Return ``ensure(shape) -> host_id``, restarting the host on change."""
    current: dict[str, str] = {}

    def _ensure(shape: str) -> str:
        if current.get("shape") != shape:
            rig.start_host(shape_providers(shape))
            current["shape"] = shape
        return rig.host_id

    return _ensure


def _create_session(
    ui: Ui,
    agent_label: str,
    *,
    pick: str | None = None,
    prompt: str = "Reply with exactly: ok. Nothing else.",
) -> tuple[str, PaneWatcher]:
    """Create a session through the landing screen, watching for its pane.

    :param ui: The browser driver, already on the landing screen.
    :param agent_label: Agent dropdown label ("Claude Code" / "Codex").
    :param pick: Optional model-dropdown entry substring to pick; ``None``
        leaves Default.
    :returns: ``(session_id, pane)`` with the pane discovered.
    """
    ui.pick_agent(agent_label)
    if pick is not None:
        ui.open_landing_config()
        ui.open_model_dropdown("new-chat-landing-config-model")
        ui.pick_dropdown_option(pick)
        ui.save_landing_config()
    pane = PaneWatcher()
    pane.arm()
    session_id = ui.submit_new_chat(prompt)
    pane.wait_for_pane()
    return session_id, pane


# ---------------------------------------------------------------------------
# Shape: claude · subscription
# ---------------------------------------------------------------------------


def test_row1_claude_picker_lists_resolved_versioned_names(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """New chat → claude dropdown shows resolved names, never "Sonnet 4.6"."""
    require_clis("claude")
    shaped_host("claude-subscription")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        ui.pick_agent("Claude Code")
        ui.open_landing_config()
        options = ui.open_model_dropdown("new-chat-landing-config-model")

    rows = [opt for opt in options if not opt.lower().startswith("default")]
    assert rows, "the claude model dropdown offered no rows at all"
    assert not any("4.6" in opt for opt in options), (
        f"a frozen 'Sonnet 4.6' label leaked into the picker: {options}"
    )
    versioned = [opt for opt in rows if _VERSIONED.search(opt)]
    assert versioned, (
        f"no row carries a resolved version — the static alias table is still "
        f"serving the picker: {options}"
    )
    assert any("1M context" in opt for opt in rows), (
        f"the 1M-context variants are missing from the probed catalog: {options}"
    )


def test_row6_claude_default_label_names_the_true_default(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """The claude picker reads "Default (X)" where X is what a bare launch runs."""
    require_clis("claude")
    shaped_host("claude-subscription")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        ui.pick_agent("Claude Code")
        ui.open_landing_config()
        label = ui.wait_landing_model_label(r"Default \(.+\)|Models unavailable")

    assert re.fullmatch(r"Default \(.+\)", label), (
        f"claude's Default entry reads {label!r} — the truthful default name is "
        "missing (the web discards isDefault, or no row carries it)"
    )


def test_row7_default_create_chip_equals_pane_truth(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """A Default create's composer chip appears and matches the pane footer."""
    require_clis("claude")
    shaped_host("claude-subscription")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        session_id, pane = _create_session(ui, "Claude Code")
        chip = ui.wait_composer_label(r"\S", timeout_s=90)
        pane_text = pane.wait_for_text(r"│")

    # The pane footer names the model on its status line; the chip must carry
    # the same family AND version (e.g. footer "Opus 4.8 (1M context)" must not
    # pair with a chip claiming "Opus 5 ...").
    footer_line = next(
        (line for line in pane_text.splitlines() if "│" in line and "context" in line.lower()),
        None,
    ) or next((line for line in pane_text.splitlines() if "│" in line), "")
    footer_model = footer_line.split("│")[0].strip()
    assert footer_model, f"could not read a model from the pane footer: {pane_text[-400:]}"
    family = footer_model.split()[0]
    assert family.lower() in chip.lower(), (
        f"composer chip {chip!r} does not carry the pane's model family {footer_model!r}"
    )
    version = re.search(r"\d+(\.\d+)?", footer_model)
    if version:
        assert version.group(0) in chip, (
            f"composer chip {chip!r} claims a different version than the pane "
            f"footer {footer_model!r} (session {session_id})"
        )


def test_row13_reported_model_renders_verbatim_not_family_collapsed(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """The gear highlights exactly the model the pane reports — generation too.

    On this machine a bare subscription launch runs the settings-file pin
    (Opus 4.8 (1M)); the picker must render that exact model as selected (as
    its own appended row when the alias catalog lacks it), never relabel it as
    the alias catalog's Opus 5.
    """
    require_clis("claude")
    shaped_host("claude-subscription")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        session_id, pane = _create_session(ui, "Claude Code")
        pane_text = pane.wait_for_text(r"│")
        footer_line = next((line for line in pane_text.splitlines() if "│" in line), "")
        footer_model = footer_line.split("│")[0].strip()
        version = re.search(r"\d+(\.\d+)?", footer_model)
        ui.wait_composer_label(r"\S", timeout_s=90)
        ui.open_gear()
        rows = ui.gear_rows()

    active = [row for row in rows if row["active"] == "true"]
    assert active, f"no gear row is highlighted (rows: {rows}, session {session_id})"
    if version:
        assert version.group(0) in active[0]["text"], (
            f"the highlighted row {active[0]} claims a different generation than "
            f"the pane's {footer_model!r} — the mirror family-collapsed the report"
        )


def test_row16_terminal_switch_mirrors_exactly(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """Typing /model haiku in the pane highlights exactly Haiku 4.5 in the web."""
    require_clis("claude")
    shaped_host("claude-subscription")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        session_id, pane = _create_session(ui, "Claude Code")
        pane.wait_for_text(r"│")
        dismiss_blocking_dialogs(pane)
        pane.type_line("/model haiku")
        pane.wait_for_text(r"Haiku", timeout=30)
        chip = ui.wait_composer_label(r"[Hh]aiku", timeout_s=30)

    assert "haiku" in chip.lower(), f"chip never mirrored the terminal switch: {chip!r}"
    snapshot = session_snapshot(rig.base_url, session_id)
    reported = snapshot.get("llm_model") or ""
    assert "haiku" in str(reported).lower(), (
        f"the session record never learned the terminal-side switch "
        f"(llm_model={reported!r}, model_override={snapshot.get('model_override')!r})"
    )


def test_row14_web_switch_confirms_against_the_pane(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """A gear pick flips the chip only once the pane actually switched."""
    require_clis("claude")
    shaped_host("claude-subscription")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        session_id, pane = _create_session(ui, "Claude Code")
        pane.wait_for_text(r"│")
        dismiss_blocking_dialogs(pane)
        ui.wait_composer_label(r"\S", timeout_s=90)
        ui.open_gear()
        ui.gear_rows()
        ui.pick_dropdown_option("Sonnet 5")
        ui.save_gear()
        chip = ui.wait_composer_label(r"[Ss]onnet", timeout_s=45)
        pane_text = pane.wait_for_text(r"Sonnet", timeout=45)

    assert "sonnet" in chip.lower(), f"the chip never settled on the pick: {chip!r}"
    assert "Sonnet" in pane_text, (
        f"the pane never switched although the web claims it did (session {session_id})"
    )


def test_row15_swallowed_injection_surfaces_an_error(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """A pane dialog eats /model → the web surfaces failure instead of lying.

    The pane is parked inside claude's own interactive /model picker dialog, so
    the injected switch command lands in that dialog and never executes. The
    design's contract: the record must not silently claim the new model — the
    web either surfaces a model_change_not_applied error or keeps reporting the
    pane's real model.
    """
    require_clis("claude")
    shaped_host("claude-subscription")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        session_id, pane = _create_session(ui, "Claude Code")
        pane.wait_for_text(r"│")
        dismiss_blocking_dialogs(pane)
        baseline = ui.wait_composer_label(r"\S", timeout_s=90)
        # Park the pane in claude's own model-picker dialog.
        pane.type_line("/model")
        time.sleep(3.0)
        ui.open_gear()
        ui.gear_rows()
        ui.pick_dropdown_option("Haiku")
        ui.save_gear()
        time.sleep(10.0)
        chip_after = ui.composer_label()
        pane_after = pane.capture()
        pane.send_key("Escape")

    pane_switched = "Haiku" in pane_after
    if not pane_switched:
        assert "haiku" not in chip_after.lower(), (
            f"the pane never switched (dialog swallowed /model) but the web now "
            f"claims Haiku — silent record/pane divergence (was {baseline!r}, "
            f"session {session_id})"
        )


def test_row17_model_and_effort_apply_together(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """One gear save changing model + effort applies both, in order."""
    require_clis("claude")
    shaped_host("claude-subscription")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        session_id, pane = _create_session(ui, "Claude Code")
        pane.wait_for_text(r"│")
        dismiss_blocking_dialogs(pane)
        ui.wait_composer_label(r"\S", timeout_s=90)
        ui.open_gear()
        ui.gear_rows()
        # Selecting an option closes the Radix dropdown itself; pressing
        # Escape afterwards would close the whole gear modal.
        ui.pick_dropdown_option("Sonnet 5")
        ui.page.get_by_test_id("composer-config-effort").click()
        # Pick a level that DIFFERS from the ambient default (this
        # machine runs "high" globally): the save skips unchanged knobs.
        ui.pick_dropdown_option("Medium")
        ui.save_gear()
        pane_text = pane.wait_for_text(r"Sonnet", timeout=45)
        # The save serializes its legs IN THE PAGE and the model leg holds
        # until the pane CONFIRMS the switch (the design's step-6 contract),
        # so the effort PATCH is sent by the browser seconds after "Sonnet"
        # appears — the browser must stay open while we poll for it.
        effort = wait_for(
            lambda: session_snapshot(rig.base_url, session_id).get("reasoning_effort"),
            timeout=45.0,
            what="the effort override to persist",
        )

    assert "Sonnet" in pane_text
    assert str(effort).lower() == "medium", f"the effort half of the save was lost: {effort!r}"


# ---------------------------------------------------------------------------
# Shape: claude · gateway kind
# ---------------------------------------------------------------------------


def test_row4_claude_gateway_rows_are_labeled_not_raw(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """The gateway picker shows harness-labeled rows, never a raw wire id."""
    require_clis("claude")
    shaped_host("claude-gateway")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        ui.pick_agent("Claude Code")
        ui.open_landing_config()
        options = ui.open_model_dropdown("new-chat-landing-config-model")

    rows = [opt for opt in options if not opt.lower().startswith("default")]
    assert rows, "the gateway claude picker offered no rows"
    raw = [opt for opt in rows if "system.ai." in opt or "databricks-" in opt]
    assert not raw, f"raw wire spellings render as display names on the gateway shape: {raw}"


def test_row8_explicit_pick_runs_exactly_the_pick(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """Creating with an explicit pick launches the pane on exactly that model."""
    require_clis("claude")
    shaped_host("claude-gateway")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        ui.pick_agent("Claude Code")
        ui.open_landing_config()
        options = ui.open_model_dropdown("new-chat-landing-config-model")
        rows = [opt for opt in options if not opt.lower().startswith("default")]
        assert rows, "no rows to pick from"
        ui.pick_dropdown_option(rows[0])
        ui.save_landing_config()
        pane = PaneWatcher()
        pane.arm()
        session_id = ui.submit_new_chat("Reply with exactly: ok. Nothing else.")
        pane.wait_for_pane()
        pane_text = pane.wait_for_text(r"│")

    footer_line = next((line for line in pane_text.splitlines() if "│" in line), "")
    footer_model = footer_line.split("│")[0].strip()
    picked_version = re.search(r"\d+(\.\d+)?", rows[0])
    if picked_version:
        assert picked_version.group(0) in footer_model, (
            f"picked {rows[0]!r} but the pane runs {footer_model!r} "
            f"(session {session_id}) — the launch substituted another model"
        )


# ---------------------------------------------------------------------------
# Shape: codex · databricks kind (gateway)
# ---------------------------------------------------------------------------


def test_row2_codex_databricks_picker_is_populated_and_decorated(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """The codex gateway dropdown is non-empty with decorated names + default."""
    require_clis("codex")
    shaped_host("codex-databricks")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        ui.pick_agent("Codex")
        ui.open_landing_config()
        label = ui.wait_landing_model_label(r"Default \(.+\)|Models unavailable")
        options = ui.open_model_dropdown("new-chat-landing-config-model")

    rows = [opt for opt in options if not opt.lower().startswith("default")]
    assert rows, "the codex databricks-kind picker is EMPTY — finding C's failure mode"
    assert any("GPT" in opt for opt in rows), (
        f"rows are not decorated with codex's display names: {options}"
    )
    assert re.fullmatch(r"Default \(.+\)", label), (
        f"no truthful default label on the codex gateway shape: {label!r}"
    )


def test_row5_host_boot_is_warm_and_nonblocking(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """Host boot: online immediately; picker rows present with no session."""
    require_clis("codex", "claude")
    started = time.monotonic()
    host_id = shaped_host("codex-databricks")
    online_after = time.monotonic() - started
    # Registration must not wait on the probes (claude's alone is ~6s+).
    assert online_after < 45.0, f"host took {online_after:.1f}s to register"

    def _codex_rows() -> list | None:
        import httpx

        try:
            payload = host_model_options(rig.base_url, host_id, "codex-native")
        except httpx.HTTPError:
            # A failed/erroring answer is a not-yet (or never) — keep polling;
            # the timeout records the red.
            return None
        return payload.get("models") or None

    rows = wait_for(_codex_rows, timeout=120.0, what="boot-warmed codex rows")
    assert rows, "the boot probe never produced codex rows"


def test_row11_gear_rows_equal_new_chat_rows(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """Picker↔gear parity: the in-session gear offers the new-chat rows."""
    require_clis("codex")
    host_id = shaped_host("codex-databricks")
    prelaunch = {
        row["id"]
        for row in host_model_options(rig.base_url, host_id, "codex-native").get("models", [])
        if isinstance(row, dict) and row.get("id")
    }
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        session_id, pane = _create_session(ui, "Codex")
        pane.wait_for_text(r"›")
        ui.wait_composer_label(r"\S", timeout_s=90)
        ui.open_gear()
        gear = {row["id"] for row in ui.gear_rows()}

    assert prelaunch, "pre-launch rows empty; parity is unmeasurable"
    missing = prelaunch - gear
    assert not missing, f"gear lacks pre-launch rows {missing} (gear={gear}, session {session_id})"


def test_row7_codex_chip_matches_config_copy_and_pane(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """Codex Default create: chip, config-copy pin, and pane footer agree."""
    require_clis("codex")
    shaped_host("codex-databricks")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        session_id, pane = _create_session(ui, "Codex")
        pane_text = pane.wait_for_text(r"›")
        chip = ui.wait_composer_label(r"\S", timeout_s=90)

    footer = next(
        (line.strip() for line in reversed(pane_text.splitlines()) if "default" in line.lower()),
        "",
    )
    pinned = codex_config_copy_model(session_id)
    assert pinned, "the launch left no model = pin in the private config copy"
    comparable = pinned.replace("databricks-", "").replace("-", "").replace(".", "").lower()
    chip_flat = chip.replace("-", "").replace(".", "").lower()
    assert comparable in chip_flat or chip_flat in comparable, (
        f"chip {chip!r} disagrees with the config-copy pin {pinned!r} "
        f"(pane footer: {footer!r}, session {session_id})"
    )


# ---------------------------------------------------------------------------
# Shape: codex · subscription
# ---------------------------------------------------------------------------


def test_row3_codex_subscription_rows_come_from_the_account(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """The subscription picker shows the account's own catalog, marked default."""
    require_clis("codex")
    shaped_host("codex-subscription")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        ui.pick_agent("Codex")
        ui.open_landing_config()
        label = ui.landing_model_label()
        options = ui.open_model_dropdown("new-chat-landing-config-model")

    rows = [opt for opt in options if not opt.lower().startswith("default")]
    assert rows, "the codex subscription picker is empty"
    assert any("GPT" in opt for opt in rows), (
        f"subscription rows are undecorated ids — the frozen tuple, not the "
        f"account catalog: {options}"
    )
    assert re.fullmatch(r"Default \(.+\)", label), (
        f"no truthful default label on the subscription shape: {label!r}"
    )


def test_row9_codex_subscription_default_first_turn_succeeds(
    rig: ModelFlowsRig, shaped_host: Callable[[str], str]
) -> None:
    """A Default subscription create's first turn returns a reply, not a 400."""
    require_clis("codex")
    shaped_host("codex-subscription")
    with browser_ui(rig.base_url) as ui:
        ui.open_landing()
        session_id, pane = _create_session(ui, "Codex")

        def _turn_settled() -> str | None:
            text = pane.capture()
            if "invalid_request_error" in text or "not supported" in text:
                return text
            if re.search(r"^\s*•", text, re.M):
                return text
            return None

        settled = wait_for(_turn_settled, timeout=120.0, what="the first turn to settle")

    assert "invalid_request_error" not in settled and "not supported" not in settled, (
        f"the Default launch pinned a model the account cannot serve — the "
        f"stale-config-line class (finding B). Pane:\n{settled[-600:]}\n"
        f"(config-copy pin: {codex_config_copy_model(session_id)!r})"
    )


# ---------------------------------------------------------------------------
# Cold resume: the persisted pick survives the pane (claude + codex)
# ---------------------------------------------------------------------------

#: Host shape, wrapper agent, and pane-ready marker per harness.
_COLD_RESUME_HARNESSES: dict[str, tuple[str, str, str]] = {
    "claude-native": ("claude-subscription", CLAUDE_NATIVE_AGENT_NAME, r"│"),
    "codex-native": ("codex-subscription", CODEX_NATIVE_AGENT_NAME, r"›"),
}


def _resume_override(harness: str, rows: list[dict[str, Any]]) -> str | None:
    """
    The model to persist, spelled the way the harness itself reports it.

    claude: a canonical Anthropic id no row spells exactly — the plain twin of
    a listed ``[1m]`` model. The endpoint serves it (the 1M marker is a request
    flag on the same model) and the pane reports exactly that id after a
    ``/model`` to it; only the catalog's alias rows know its family.

    codex: a non-default row id — codex has no alias layer, so the harness's
    own report IS a catalog id.

    :param harness: ``"claude-native"`` or ``"codex-native"``.
    :param rows: The host's pre-launch catalog rows.
    :returns: The override to persist, or ``None`` when the catalog offers
        no such spelling.
    """
    listed = {str(v) for row in rows for v in (row.get("id"), row.get("model")) if v}
    if harness == "claude-native":
        for row in rows:
            model = str(row.get("model") or "")
            if model.startswith("claude-") and model.endswith("[1m]") and model[:-4] not in listed:
                return model[:-4]
        return None
    for row in rows:
        if row.get("isDefault") is True or row.get("hidden") is True:
            continue
        row_id = row.get("id")
        if isinstance(row_id, str) and row_id:
            return row_id
    return None


def _same_model(reported: object, wanted: str) -> bool:
    """
    Whether a harness report names *wanted* (a dated suffix allowed)."""
    if not isinstance(reported, str) or not reported:
        return False
    lhs, rhs = reported.lower(), wanted.lower()
    return lhs == rhs or lhs.startswith(f"{rhs}-")


@pytest.mark.timeout(900)
@pytest.mark.parametrize("harness", sorted(_COLD_RESUME_HARNESSES))
def test_row18_cold_resume_honors_the_persisted_model_override(
    rig: ModelFlowsRig,
    shaped_host: Callable[[str], str],
    harness: str,
    tmp_path: Path,
) -> None:
    """
    A session resumes on the persisted model the harness was already running.

    The pane exits (idle reap, host restart) and the next message re-creates
    the terminal. The relaunch must pass the persisted model straight through
    — the host demonstrably served it moments earlier — instead of refusing
    it as "not in this host's current model list" because the catalog spells
    the same model differently (an alias row carries the family, the override
    the canonical id).
    """
    shape, agent_name, ready = _COLD_RESUME_HARNESSES[harness]
    require_clis(harness.split("-")[0])
    host_id = shaped_host(shape)

    def _rows() -> list[dict[str, Any]] | None:
        try:
            payload = host_model_options(rig.base_url, host_id, harness)
        except httpx.HTTPError:
            return None
        return payload.get("models") or None

    rows = wait_for(_rows, timeout=180.0, what=f"the boot-warmed {harness} catalog")
    override = _resume_override(harness, rows)
    if override is None:
        pytest.skip(f"this {harness} catalog offers no spelling to exercise: {rows}")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "README.md").write_text("# cold resume workspace\n")
    pane = PaneWatcher()
    pane.arm()
    session_id = rest_create_session(
        rig.base_url,
        agent_name=agent_name,
        host_id=host_id,
        workspace=workspace,
        terminal_launch_args=bypass_args(harness),
    )
    rest_post_user_message(rig.base_url, session_id, "Reply with exactly: ok. Nothing else.")
    pane.wait_for_pane()
    pane.wait_for_text(ready)
    dismiss_blocking_dialogs(pane)

    def _replies(at_least: int) -> Callable[[], int | None]:
        def _count() -> int | None:
            snapshot = session_snapshot(rig.base_url, session_id)
            assert snapshot.get("status") != "failed", (
                f"session {session_id} failed: "
                f"{snapshot.get('last_task_error') or snapshot.get('error')}"
            )
            count = assistant_message_count(snapshot)
            return count if count >= at_least else None

        return _count

    def _reported_override() -> str | None:
        reported = session_snapshot(rig.base_url, session_id).get("llm_model")
        return str(reported) if _same_model(reported, override) else None

    wait_for(_replies(1), timeout=150.0, what="the first reply")

    # The actor is a REPL ``/model``, a routing pin, or an API client: they
    # persist the harness's own spelling. The pane switches live — this host
    # serves the model — and reports it back verbatim.
    rest_patch_session(rig.base_url, session_id, model_override=override)
    rest_post_user_message(rig.base_url, session_id, "Reply with exactly: switched. Nothing else.")
    wait_for(_replies(2), timeout=150.0, what="the reply on the switched model")
    wait_for(_reported_override, timeout=60.0, what=f"the harness to report {override!r}")

    # The pane exits; the next message is the resume.
    kill_pane(pane)
    resumed = PaneWatcher()
    resumed.arm()
    rest_post_user_message(rig.base_url, session_id, "Reply with exactly: back. Nothing else.")
    wait_for(_replies(3), timeout=240.0, what="the reply after the cold resume")

    snapshot = session_snapshot(rig.base_url, session_id)
    assert snapshot.get("model_override") == override, (
        f"the resume rewrote the persisted pick: {snapshot.get('model_override')!r}"
    )
    assert _same_model(snapshot.get("llm_model"), override), (
        f"the resumed pane runs {snapshot.get('llm_model')!r}, not the persisted "
        f"{override!r} (session {session_id})"
    )
    resumed.wait_for_pane()
    pane_text = resumed.wait_for_text(ready)
    if harness == "codex-native":
        pinned = codex_config_copy_model(session_id)
        assert pinned == override, (
            f"the relaunch pinned {pinned!r} in the config copy, not {override!r}"
        )
    else:
        family = override.removeprefix("claude-").split("-")[0]
        footer = next((line for line in pane_text.splitlines() if "│" in line), "")
        assert family in footer.lower(), (
            f"the resumed pane footer {footer!r} does not name the {family} family "
            f"of {override!r} (session {session_id})"
        )


@pytest.mark.timeout(900)
def test_row18b_codex_cold_resume_honors_a_gateway_spelled_override(
    rig: ModelFlowsRig,
    shaped_host: Callable[[str], str],
    tmp_path: Path,
) -> None:
    """
    A codex resume honors an override stored in the gateway spelling.

    The realistic bite is a databricks-profile session whose persisted
    ``/model`` is a gateway id (``databricks-gpt-5-6-sol``) while the catalog
    lists codex's own slug (``gpt-5.6-sol``). The launch translates between the
    two, so the relaunch must fold the spellings rather than refuse the model
    as "not in this host's current model list" (a raw string-equality check
    did, failing the whole cold resume).
    """
    from omnigent.codex_model_vocabulary import codex_reachable_model_slug, comparable_model_id
    from omnigent.model_catalog_store import catalog_contains

    harness = "codex-native"
    ready = _COLD_RESUME_HARNESSES[harness][2]
    require_clis("codex")
    host_id = shaped_host("codex-databricks")

    def _rows() -> list[dict[str, Any]] | None:
        try:
            payload = host_model_options(rig.base_url, host_id, harness)
        except httpx.HTTPError:
            return None
        return payload.get("models") or None

    rows = wait_for(_rows, timeout=180.0, what="the boot-warmed codex-databricks catalog")
    # A non-default codex-slug row, respelled the gateway way. Only meaningful
    # when the catalog spells it as a codex slug and the raw check misses it.
    slug = next(
        (
            str(r["id"])
            for r in rows
            if r.get("id") and not r.get("isDefault") and not r.get("hidden")
        ),
        None,
    )
    if slug is None:
        pytest.skip(f"this catalog offers no non-default row to respell: {rows}")
    override = "databricks-" + slug.replace(".", "-")
    if catalog_contains(rows, override) or codex_reachable_model_slug(override, rows) is None:
        pytest.skip(
            f"the gateway respelling {override!r} does not exercise the fold "
            f"against this catalog: {rows}"
        )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "README.md").write_text("# gateway-spelled cold resume\n")
    pane = PaneWatcher()
    pane.arm()
    session_id = rest_create_session(
        rig.base_url,
        agent_name=CODEX_NATIVE_AGENT_NAME,
        host_id=host_id,
        workspace=workspace,
        terminal_launch_args=bypass_args(harness),
    )
    rest_post_user_message(rig.base_url, session_id, "Reply with exactly: ok. Nothing else.")
    pane.wait_for_pane()
    pane.wait_for_text(ready)
    dismiss_blocking_dialogs(pane)

    def _replies(at_least: int) -> Callable[[], int | None]:
        def _count() -> int | None:
            snapshot = session_snapshot(rig.base_url, session_id)
            assert snapshot.get("status") != "failed", (
                f"session {session_id} failed: "
                f"{snapshot.get('last_task_error') or snapshot.get('error')}"
            )
            count = assistant_message_count(snapshot)
            return count if count >= at_least else None

        return _count

    def _folds_to_override() -> str | None:
        reported = session_snapshot(rig.base_url, session_id).get("llm_model")
        if isinstance(reported, str) and comparable_model_id(reported) == comparable_model_id(
            override
        ):
            return reported
        return None

    wait_for(_replies(1), timeout=150.0, what="the first reply")

    # Persist the GATEWAY spelling, the way a databricks route / API client
    # does; the live switch folds it to codex's slug and the pane reports that.
    rest_patch_session(rig.base_url, session_id, model_override=override)
    rest_post_user_message(rig.base_url, session_id, "Reply with exactly: switched. Nothing else.")
    wait_for(_replies(2), timeout=150.0, what="the reply on the switched model")
    wait_for(_folds_to_override, timeout=60.0, what=f"the pane to fold to {override!r}")

    # The pane exits; the next message is the resume. Pre-fix, the relaunch's
    # raw catalog check refused the gateway-spelled override and the session
    # failed here.
    kill_pane(pane)
    resumed = PaneWatcher()
    resumed.arm()
    rest_post_user_message(rig.base_url, session_id, "Reply with exactly: back. Nothing else.")
    wait_for(_replies(3), timeout=240.0, what="the reply after the cold resume")

    snapshot = session_snapshot(rig.base_url, session_id)
    assert snapshot.get("model_override") == override, (
        f"the resume rewrote the persisted pick: {snapshot.get('model_override')!r}"
    )
    reported = snapshot.get("llm_model")
    assert isinstance(reported, str) and comparable_model_id(reported) == comparable_model_id(
        override
    ), f"the resumed pane runs {reported!r}, not the folded {override!r} (session {session_id})"
    resumed.wait_for_pane()
    resumed.wait_for_text(ready)
    pinned = codex_config_copy_model(session_id)
    assert pinned is not None and comparable_model_id(pinned) == comparable_model_id(override), (
        f"the relaunch pinned {pinned!r} in the config copy, not a fold of {override!r}"
    )


# ---------------------------------------------------------------------------
# Stale catalog: a Default launch must not run yesterday's default
# ---------------------------------------------------------------------------


@pytest.mark.timeout(600)
def test_row19_stale_catalog_default_launch_defers_to_the_cli_default(
    rig: ModelFlowsRig,
    shaped_host: Callable[[str], str],
    tmp_path: Path,
) -> None:
    """
    A month-old catalog default must not govern a Default launch.

    The store never re-probes on its own, so its ``isDefault`` row can
    outlive the model it names (a retirement or an entitlement change).
    Pinning it as ``--model`` then hard-fails every Default session on the
    host with a provider ``model_not_found`` — sticky, because nothing
    invalidates the entry. The launch must instead defer to the CLI's own
    always-servable default and let the background re-probe converge the
    store. Seeded with Claude Code's real 2024 default, retired upstream
    today — exactly what a catalog written back then would hold.
    """
    require_clis("claude")
    host_id = shaped_host("claude-subscription")
    # Let the boot probe finish first, so it cannot overwrite the seed.
    wait_for(
        lambda: host_model_options(rig.base_url, host_id, "claude-native").get("models") or None,
        timeout=180.0,
        what="the boot-warmed claude catalog",
    )
    retired = "claude-3-5-sonnet-20241022"
    fingerprint = claude_catalog_fingerprint(None)
    path = rig.data_dir / "cache" / "model-catalogs" / f"claude-native-{fingerprint}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    month_ago = time.time() - 30 * 86400
    path.write_text(
        json.dumps(
            {
                "harness": "claude-native",
                "fingerprint": fingerprint,
                "written_at": month_ago,
                "models": [
                    {"id": "opus", "model": "claude-opus-5", "displayName": "Opus 5"},
                    {
                        "id": retired,
                        "model": retired,
                        "displayName": "Claude 3.5 Sonnet",
                        "isDefault": True,
                    },
                ],
            }
        )
    )
    os.utime(path, (month_ago, month_ago))

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "README.md").write_text("# stale catalog workspace\n")
    pane = PaneWatcher()
    pane.arm()
    session_id = rest_create_session(
        rig.base_url,
        agent_name=CLAUDE_NATIVE_AGENT_NAME,
        host_id=host_id,
        workspace=workspace,
        terminal_launch_args=bypass_args("claude-native"),
    )
    rest_post_user_message(rig.base_url, session_id, "Reply with exactly: ok. Nothing else.")

    def _settled() -> dict[str, Any] | None:
        snapshot = session_snapshot(rig.base_url, session_id)
        assert snapshot.get("status") != "failed", (
            f"the stale catalog default still governed the launch: "
            f"{snapshot.get('last_task_error') or snapshot.get('error')} "
            f"(session {session_id})"
        )
        return snapshot if assistant_message_count(snapshot) >= 1 else None

    snapshot = wait_for(_settled, timeout=240.0, what="the Default session's first reply")
    reported = str(snapshot.get("llm_model") or "")
    assert not reported.startswith(retired), (
        f"the launch still ran the stale pin {retired!r} (session {session_id})"
    )
