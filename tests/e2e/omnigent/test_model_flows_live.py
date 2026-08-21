"""Live model-flow CUJs (model-flows-design.md §10.1, the "UI-live" tier).

Every test drives the product the way a person uses it — the browser drives the
rig server's real SPA, and harness truth is read from the tmux pane — with REST
snapshots as secondary probes only. Assertions encode the DESIGN's target
behavior, so this suite is red on unmodified main (and partially red on the PR
branch) in exactly the ways `model-flows-report.md` cites, and goes green as
the landing-order steps land. Run one row's red twin with::

    OMNIGENT_E2E_MODEL_FLOWS=1 OMNIGENT_E2E_MODEL_FLOWS_REPO=~/omnigent \\
        pytest tests/e2e/omnigent/test_model_flows_live.py -k row1 -x

Rows are numbered after the design's §10.1 table.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator

import pytest

from tests.e2e.omnigent._model_flows_rig import (
    ModelFlowsRig,
    PaneWatcher,
    Ui,
    booted_rig,
    browser_ui,
    codex_config_copy_model,
    dismiss_blocking_dialogs,
    host_model_options,
    require_clis,
    require_opt_in,
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
