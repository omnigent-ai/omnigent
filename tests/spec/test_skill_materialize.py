import concurrent.futures
from pathlib import Path

from omnigent.spec.skill_materialize import materialize_for_harness, projection_root_for_harness
from omnigent.spec.skill_registry import SkillCandidate, SkillEntry, tree_digest
from omnigent.spec.types import SkillSpec


def _entry(tmp_path: Path, name: str) -> SkillEntry:
    source = tmp_path / "sources" / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(name)
    skill = SkillSpec(name=name, description=name, content=name, skill_dir=source)
    candidate = SkillCandidate(
        provider="bundle",
        location_scope="bundle",
        source_kind="bundled",
        origin_path=source,
        source_coords=f"bundle:{name}",
        namespace="bundle",
        invocation_name=name,
        managed=True,
        tree_digest=tree_digest(source),
        skill=skill,
    )
    return SkillEntry(candidate, name)


def test_materialize_is_atomic_idempotent_and_prunes_stale(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    first = _entry(tmp_path, "first")
    second = _entry(tmp_path, "second")
    assert materialize_for_harness([first, second], "codex-native", session_dir)
    assert not materialize_for_harness([first, second], "codex-native", session_dir)
    assert materialize_for_harness([second], "codex-native", session_dir)
    root = projection_root_for_harness("codex-native", session_dir)
    assert not (root / "first").exists()
    assert (root / "second").resolve() == second.winner.origin_path.resolve()


def test_materialize_serializes_concurrent_spawns(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    entries = [_entry(tmp_path, "one"), _entry(tmp_path, "two")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        list(
            executor.map(
                lambda _: materialize_for_harness(entries, "claude-native", session_dir),
                range(8),
            )
        )
    root = projection_root_for_harness("claude-native", session_dir)
    assert sorted(path.name for path in root.iterdir() if not path.name.startswith(".")) == [
        "one",
        "two",
    ]
