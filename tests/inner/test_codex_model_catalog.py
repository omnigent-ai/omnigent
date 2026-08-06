"""The session-private codex model catalog that makes GLM spawnable.

Codex validates ``spawn_agent``'s ``model`` against its own model catalog
before any request leaves the CLI, and ``model_catalog_json`` REPLACES that
catalog rather than merging into it — so the file omnigent writes has to be
codex's own catalog plus the gateway-only arms.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from omnigent.codex_model_vocabulary import EXTENDED_CATALOG_MODELS
from omnigent.inner import codex_executor
from omnigent.inner.codex_executor import (
    extended_model_catalog,
    set_codex_model_catalog_path,
    write_codex_model_catalog,
)

_GLM_SLUG = EXTENDED_CATALOG_MODELS["glm-5-2"]


def _catalog(**overrides: Any) -> dict[str, Any]:  # type: ignore[explicit-any]
    luna: dict[str, Any] = {  # type: ignore[explicit-any]
        "slug": "gpt-5.6-luna",
        "display_name": "GPT-5.6-Luna",
        "description": "Fast and affordable agentic coding model.",
        "visibility": "list",
        "context_window": 272000,
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": "low"},
            {"effort": "medium"},
            {"effort": "high"},
            {"effort": "xhigh"},
        ],
        "availability_nux": {"seen": 3},
        "upgrade": {"to": "gpt-5.6-sol"},
        "base_instructions": "You are Codex...",
        **overrides,
    }
    return {"models": [{"slug": "gpt-5.6-sol", "visibility": "list"}, luna]}


def test_the_added_entry_carries_the_clone_sources_fields() -> None:
    extended = extended_model_catalog(_catalog())

    assert extended is not None
    glm = next(m for m in extended["models"] if m["slug"] == _GLM_SLUG)
    # Cloned, so codex has every field it needs without omnigent pinning a
    # vendor prompt or a context window of its own.
    assert glm["base_instructions"] == "You are Codex..."
    assert glm["context_window"] == 272000
    # Spawnable: the enum is the entries whose visibility lists them.
    assert glm["visibility"] == "list"
    # Upsell metadata described the cloned arm, not this one.
    assert glm["availability_nux"] is None
    assert glm["upgrade"] is None


def test_the_added_entry_declares_only_the_efforts_glm_accepts() -> None:
    # Codex refuses a pairing outside the ladder ("Reasoning effort `xhigh` is
    # not supported for model `system.ai.glm-5-2`"), and applies the default
    # when a spawn names no effort — so both belong to the entry.
    extended = extended_model_catalog(_catalog())

    assert extended is not None
    glm = next(m for m in extended["models"] if m["slug"] == _GLM_SLUG)
    assert [lv["effort"] for lv in glm["supported_reasoning_levels"]] == [
        "low",
        "medium",
        "high",
    ]
    assert glm["default_reasoning_level"] == "medium"


def test_the_bundled_arms_survive() -> None:
    # ``model_catalog_json`` replaces the bundled catalog, so dropping an arm
    # here would make it unspawnable.
    extended = extended_model_catalog(_catalog())

    assert extended is not None
    assert [m["slug"] for m in extended["models"]] == [
        "gpt-5.6-sol",
        "gpt-5.6-luna",
        _GLM_SLUG,
    ]


@pytest.mark.parametrize(
    "catalog",
    [
        # Nothing to clone from: leave codex on its own catalog rather than
        # writing one that would narrow the spawn enum.
        {"models": [{"slug": "gpt-5.6-sol"}]},
        {"models": []},
        {},
        {"models": "not a list"},
    ],
)
def test_an_unusable_catalog_is_left_alone(
    catalog: dict[str, Any],  # type: ignore[explicit-any]
) -> None:
    assert extended_model_catalog(catalog) is None


def test_an_arm_codex_already_carries_is_not_duplicated() -> None:
    assert extended_model_catalog(_catalog(slug=_GLM_SLUG)) is None


def test_write_is_skipped_without_a_codex_binary(tmp_path: Path) -> None:
    assert write_codex_model_catalog(tmp_path, codex_path=None, source_home=tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_the_cli_is_probed_once_per_host_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The probe blocks the event loop during session boot, and the catalog is a
    # property of the installed CLI — so every session after the first reuses it.
    calls: list[str] = []

    def _probe(codex_path: str, source_home: Path, *, timeout: float) -> dict[str, Any]:  # type: ignore[explicit-any]
        del source_home, timeout
        calls.append(codex_path)
        return _catalog()

    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_CACHE", {})
    monkeypatch.setattr(codex_executor, "_probe_codex_model_catalog", _probe)

    for _ in range(3):
        assert codex_executor.read_codex_model_catalog("/bin/codex", tmp_path) is not None
    assert calls == ["/bin/codex"]


def test_an_in_place_codex_upgrade_is_re_probed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The catalog is the INSTALLED binary's, and an upgrade replaces it in place.

    ``npm i -g @openai/codex`` keeps the same path, so a path-only cache key
    served the old codex's models for the rest of the host process — including
    the arms a newer codex added.
    """
    calls: list[str] = []

    def _probe(codex_path: str, source_home: Path, *, timeout: float) -> dict[str, Any]:  # type: ignore[explicit-any]
        del source_home, timeout
        calls.append(codex_path)
        return _catalog()

    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_CACHE", {})
    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_FAILURES", {})
    monkeypatch.setattr(codex_executor, "_probe_codex_model_catalog", _probe)

    binary = tmp_path / "codex"
    binary.write_text("v1")
    home = tmp_path / "home"
    assert codex_executor.read_codex_model_catalog(str(binary), home) is not None
    assert codex_executor.read_codex_model_catalog(str(binary), home) is not None
    assert len(calls) == 1

    # Upgraded in place: same path, different bytes.
    binary.write_text("v2-and-then-some")
    assert codex_executor.read_codex_model_catalog(str(binary), home) is not None
    assert len(calls) == 2


def test_a_cli_that_cannot_answer_is_not_re_probed_within_the_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A broken CLI must not cost the full timeout per session, so the failure is
    # remembered — but only briefly (see the test below).
    calls: list[str] = []

    def _probe(codex_path: str, source_home: Path, *, timeout: float) -> None:
        del source_home, timeout
        calls.append(codex_path)

    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_CACHE", {})
    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_FAILURES", {})
    monkeypatch.setattr(codex_executor, "_probe_codex_model_catalog", _probe)

    for _ in range(3):
        assert codex_executor.read_codex_model_catalog("/bin/codex", tmp_path) is None
    assert calls == ["/bin/codex"]


def test_a_transient_probe_failure_is_retried_after_the_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    One 10 s timeout must not disable the catalog for the whole process.

    The failure used to be cached permanently, so a single slow probe — a loaded
    host, a cold binary — meant every LATER session on that host silently lost
    the gateway-only arms from its ``spawn_agent`` catalog, with nothing in the
    logs after the first warning to say why.
    """
    results: list[dict[str, Any] | None] = [None, _catalog()]
    calls: list[str] = []

    def _probe(codex_path: str, source_home: Path, *, timeout: float) -> dict[str, Any] | None:  # type: ignore[explicit-any]
        del source_home, timeout
        calls.append(codex_path)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_CACHE", {})
    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_FAILURES", {})
    monkeypatch.setattr(codex_executor, "_probe_codex_model_catalog", _probe)

    assert codex_executor.read_codex_model_catalog("/bin/codex", tmp_path) is None
    # Inside the TTL: no re-probe.
    assert codex_executor.read_codex_model_catalog("/bin/codex", tmp_path) is None
    assert len(calls) == 1

    # The TTL lapses.
    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_FAILURE_TTL_S", 0.0)
    codex_executor._MODEL_CATALOG_FAILURES[
        codex_executor._model_catalog_cache_key("/bin/codex", tmp_path)
    ] = time.monotonic() - 1.0

    assert codex_executor.read_codex_model_catalog("/bin/codex", tmp_path) is not None
    assert len(calls) == 2
    # And the success is remembered for good.
    assert codex_executor.read_codex_model_catalog("/bin/codex", tmp_path) is not None
    assert len(calls) == 2


def test_the_catalog_probe_runs_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``codex debug models`` is a ~10 s subprocess, so it must not run inline.

    Both async callers populate the private CODEX_HOME (which is what shells out
    to the probe) through ``asyncio.to_thread``; run on the loop it stalled every
    other session sharing it for the probe's whole timeout.
    """
    import inspect

    from omnigent import codex_native_app_server

    for module in (codex_executor, codex_native_app_server):
        source = inspect.getsource(module)
        assert "await asyncio.to_thread(\n            _populate_codex_home_config," in source, (
            f"{module.__name__} must populate the codex home off the event loop"
        )
        assert "\n        _populate_codex_home_config(" not in source
    del tmp_path, monkeypatch


@pytest.mark.parametrize(
    "stdout",
    [
        # Not JSON at all — a codex that printed a banner or an error.
        "not json",
        # JSON, but not an object.
        "[]",
        # Right shape, empty or unusable entries: an entry with no slug cannot
        # be matched or cloned, and codex would accept only what this file
        # lists — so a partially-readable payload would NARROW the spawn enum.
        '{"models": []}',
        '{"models": [{"slug": "gpt-5.6-luna"}, {"display_name": "no slug"}]}',
        '{"models": [{"slug": ""}]}',
        '{"models": [{"slug": "gpt-5.6-luna"}, "a string"]}',
    ],
)
def test_a_malformed_probe_result_keeps_the_bundled_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
) -> None:
    """``model_catalog_json`` replaces codex's catalog, so a bad probe must not
    become that file — the session keeps codex's bundled one (fail open)."""

    class _Completed:
        returncode = 0
        stderr = ""

        def __init__(self, out: str) -> None:
            self.stdout = out

    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_CACHE", {})
    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_FAILURES", {})
    monkeypatch.setattr(
        codex_executor.subprocess,
        "run",
        lambda *a, **k: _Completed(stdout),  # type: ignore[arg-type]
    )

    assert codex_executor.read_codex_model_catalog("/bin/codex", tmp_path) is None
    written = write_codex_model_catalog(tmp_path, codex_path="/bin/codex", source_home=tmp_path)
    assert written is None
    assert not (tmp_path / "model_catalog.json").exists()


def test_concurrent_populates_share_one_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sessions booting together must not each pay the 10 s probe.

    Both callers reach the catalog from a worker thread (the populate runs
    through ``asyncio.to_thread``), so without a lock they raced the cache and
    shelled out twice.
    """
    import threading

    calls: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def _probe(codex_path: str, source_home: Path, *, timeout: float) -> dict[str, Any]:  # type: ignore[explicit-any]
        del source_home, timeout
        calls.append(codex_path)
        entered.set()
        # Hold the probe open so the second thread is guaranteed to arrive
        # while the first is still inside it — the race window itself.
        release.wait(timeout=10)
        return _catalog()

    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_CACHE", {})
    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_FAILURES", {})
    monkeypatch.setattr(codex_executor, "_probe_codex_model_catalog", _probe)

    results: list[dict[str, Any] | None] = []  # type: ignore[explicit-any]

    def _read() -> None:
        results.append(codex_executor.read_codex_model_catalog("/bin/codex", tmp_path))

    first = threading.Thread(target=_read)
    first.start()
    assert entered.wait(timeout=10)
    second = threading.Thread(target=_read)
    second.start()
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert calls == ["/bin/codex"]
    assert all(result is not None for result in results)


def test_the_config_key_lands_above_the_first_table(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "databricks-gpt-5-6-luna"\n\n[model_providers.Databricks]\nname = "x"\n'
    )
    catalog = tmp_path / "model_catalog.json"
    catalog.write_text("{}")

    assert set_codex_model_catalog_path(config, catalog) is True
    lines = config.read_text().splitlines()
    key_at = next(i for i, line in enumerate(lines) if line.startswith("model_catalog_json"))
    table_at = next(i for i, line in enumerate(lines) if line.startswith("["))
    # A top-level key inside a table would configure the provider, not codex.
    assert key_at < table_at
    assert json.loads(lines[key_at].split("=", 1)[1].strip()) == str(catalog)


def test_a_users_own_catalog_choice_wins(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('model_catalog_json = "/mine.json"\n')

    assert set_codex_model_catalog_path(config, tmp_path / "ours.json") is False
    assert config.read_text() == 'model_catalog_json = "/mine.json"\n'
