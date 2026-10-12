"""Keep legacy GitHub readers isolated from other providers during a downgrade."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from filelock import FileLock

from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry, SessionPullRequest

GITHUB = "https://github.com/example/repo/pull/1"
FOREIGN = "https://forge.example.com/team/repo/requests/2"


@pytest.fixture
def references(monkeypatch: pytest.MonkeyPatch) -> list[PullRequestRef]:
    github = PullRequestRef.from_url(GITHUB)
    foreign = PullRequestRef(
        provider="example",
        host="forge.example.com",
        repository="team/repo",
        number=2,
        url=FOREIGN,
    )
    original = PullRequestRef.from_url
    monkeypatch.setattr(
        PullRequestRef,
        "from_url",
        classmethod(lambda cls, url: foreign if url == FOREIGN else original(url)),
    )
    return [github, foreign]


def _seed_mixed(store: SessionPrRegistry, references: list[PullRequestRef]) -> None:
    store.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "prs": [
                    SessionPullRequest(
                        **ref.model_dump(),
                        relationship="created",
                        source="native",
                        first_seen_at=10,
                        last_seen_at=20,
                        title="Cached title",
                        title_checked_at=30,
                    ).model_dump()
                    for ref in references
                ],
                "excluded": ["https://github.com/example/repo/pull/99"],
                "observations": ["already-seen"],
            }
        )
    )


def test_legacy_file_contains_only_github(tmp_path: Path, references) -> None:
    store = SessionPrRegistry("mixed", root=tmp_path)
    store.record(references, relationship="created", source="native", timestamp=10)
    assert [pr["url"] for pr in json.loads(store.path.read_text())["prs"]] == [GITHUB]
    assert {pr.url for pr in SessionPrRegistry("mixed", root=tmp_path).list()} == {GITHUB, FOREIGN}
    assert store.path.with_suffix(".providers.json").stat().st_mode & 0o777 == 0o600


def test_github_only_does_not_need_companion_file(tmp_path: Path, references) -> None:
    store = SessionPrRegistry("github", root=tmp_path)
    store.record(references[:1], relationship="attached", source="user")
    assert not store.path.with_suffix(".providers.json").exists()
    assert [pr.url for pr in store.list()] == [GITHUB]


@pytest.mark.parametrize("relationship", ["created", "inferred"])
def test_equal_timestamp_order_survives_split(tmp_path: Path, references, relationship) -> None:
    store = SessionPrRegistry("mixed", root=tmp_path)
    store.record(references[::-1], relationship=relationship, source="native", timestamp=10)
    assert [pr.url for pr in store.list()] == [FOREIGN, GITHUB]
    store.update_titles({FOREIGN: "Foreign title", GITHUB: "GitHub title"})
    assert [pr.url for pr in SessionPrRegistry("mixed", root=tmp_path).list()] == [FOREIGN, GITHUB]


def test_legacy_github_write_preserves_foreign_state(tmp_path: Path, references) -> None:
    store = SessionPrRegistry("mixed", root=tmp_path)
    store.record(references, relationship="created", source="native", observation_id="first")
    store.update_titles({FOREIGN: "Foreign title"}, timestamp=100)
    foreign = next(pr for pr in store.list() if pr.url == FOREIGN)
    # Legacy hosts rewrite only their known schema, and preserve opaque history lists.
    with FileLock(str(store.path) + ".lock"):
        legacy = json.loads(store.path.read_text())
        legacy["prs"] = []
        legacy["excluded"].append(GITHUB)
        legacy["observations"].append("legacy-write")
        store.path.write_text(json.dumps(legacy))
    assert store.list() == [foreign]
    store.record(references, relationship="worked_on", source="replay", observation_id="first")
    assert store.list() == [foreign]
    store.record(references[:1], relationship="inferred", source="branch")
    assert store.list() == [foreign]
    store.record(references[:1], relationship="attached", source="user")
    assert {pr.url for pr in store.list()} == {GITHUB, FOREIGN}
    assert "legacy-write" in json.loads(store.path.read_text())["observations"]


@pytest.mark.parametrize("operation", ["list", "replay", "titles"])
def test_mixed_file_migrates_without_losing_metadata(
    tmp_path: Path, references, operation
) -> None:
    store = SessionPrRegistry("mixed", root=tmp_path)
    _seed_mixed(store, references)
    original = json.loads(store.path.read_text())
    if operation == "list":
        store.list()
    elif operation == "replay":
        store.record(
            references, relationship="worked_on", source="replay", observation_id="already-seen"
        )
    else:
        store.update_titles({FOREIGN: "Older title"}, timestamp=1)
    assert [pr["url"] for pr in json.loads(store.path.read_text())["prs"]] == [GITHUB]
    assert {pr.url: pr.model_dump() for pr in store.list()} == {
        pr["url"]: pr for pr in original["prs"]
    }
    for key in ("excluded", "observations"):
        assert json.loads(store.path.read_text())[key] == original[key]


def test_migration_retries_after_interrupted_legacy_replace(tmp_path: Path, references) -> None:
    import os

    store = SessionPrRegistry("mixed", root=tmp_path)
    _seed_mixed(store, references)
    replace = os.replace

    def interrupted(src, dst):
        if dst == store.path:
            raise OSError("interrupted")
        replace(src, dst)

    with patch("omnigent.runner.session_prs.os.replace", side_effect=interrupted):
        with pytest.raises(OSError, match="interrupted"):
            store.list()
    assert store.path.with_suffix(".providers.json").exists()
    assert len(store.list()) == 2
    assert [pr["url"] for pr in json.loads(store.path.read_text())["prs"]] == [GITHUB]


def test_read_preserves_uninstalled_provider(tmp_path: Path, references) -> None:
    store = SessionPrRegistry("mixed", root=tmp_path)
    _seed_mixed(store, references)
    with patch.object(PullRequestRef, "from_url", side_effect=ValueError("not installed")):
        assert {pr.url for pr in store.list()} == {GITHUB, FOREIGN}
        store.update_titles({GITHUB: "New title"})
        assert {pr.url for pr in store.list()} == {GITHUB, FOREIGN}


def test_corrupt_companion_is_not_overwritten(tmp_path: Path, references) -> None:
    store = SessionPrRegistry("mixed", root=tmp_path)
    store.record(references, relationship="created", source="native")
    original = store.path.read_bytes()
    companion = store.path.with_suffix(".providers.json")
    companion.write_text("corrupt")
    with pytest.raises(ValueError):
        store.remove(GITHUB)
    assert companion.read_text() == "corrupt"
    assert store.path.read_bytes() == original


def test_concurrent_writers_preserve_both_providers(tmp_path: Path, references) -> None:
    def record(reference):
        SessionPrRegistry("mixed", root=tmp_path).record(
            [reference],
            relationship="created",
            source="native",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, references * 5))
    store = SessionPrRegistry("mixed", root=tmp_path)
    assert {pr.url for pr in store.list()} == {GITHUB, FOREIGN}
    assert [pr["url"] for pr in json.loads(store.path.read_text())["prs"]] == [GITHUB]


@pytest.mark.parametrize("interrupt", [False, True])
def test_foreign_removal_stays_removed_and_can_be_reattached(
    tmp_path: Path, references, interrupt: bool
) -> None:
    store = SessionPrRegistry("mixed", root=tmp_path)
    store.record(references, relationship="created", source="native", observation_id="first")
    if interrupt:
        replace = os.replace
        replacements = 0

        def interrupted(src, dst):
            nonlocal replacements
            replacements += 1
            if replacements == 2:
                raise OSError("interrupted")
            replace(src, dst)

        with patch("omnigent.runner.session_prs.os.replace", side_effect=interrupted):
            with pytest.raises(OSError, match="interrupted"):
                store.remove(FOREIGN)
    else:
        store.remove(FOREIGN)
    store = SessionPrRegistry("mixed", root=tmp_path)
    assert [pr.url for pr in store.list()] == [GITHUB]
    store.record(references, relationship="created", source="native", observation_id="first")
    store.record(references, relationship="inferred", source="branch")
    assert [pr.url for pr in store.list()] == [GITHUB]
    store.record(references, relationship="attached", source="user")
    assert {pr.url for pr in store.list()} == {GITHUB, FOREIGN}
