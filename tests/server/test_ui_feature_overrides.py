"""UI feature overrides must not alter server-gated behavior."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest

from omnigent.server.feature_flags import FEATURE_DEFINITIONS, Feature

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_OVERRIDES_SOURCE = _REPO_ROOT / "web" / "src" / "lib" / "uiFeatureOverrides.ts"
_SERVER_SOURCE = _REPO_ROOT / "omnigent"
_DEFINITIONS_FILE = Path("server/feature_flags.py")
# Mentions that name a feature without gating anything: (feature, file, line text).
_NOT_GATES = {
    # The SPA's route prefixes, so `/canvas` falls back to index.html.
    ("canvas", "server/app.py", '"canvas",'),
}


def _features_from_source(source: str) -> list[str]:
    feature_list = re.search(
        r"\bOVERRIDABLE_UI_FEATURES\b[^=]*=\s*\[(.*?)\]\s*;", source, re.DOTALL
    )
    assert feature_list is not None, "UI feature override list not found"
    list_source = feature_list.group(1)
    names = re.findall(r'\bfeature\s*:\s*"([a-z0-9_]+)"(?=\s*[,}])', list_source)
    keys = re.findall(r"\bfeature\s*:", list_source)
    assert len(names) == len(keys), "Every override entry must have a static feature name"
    return names


def _overridable_features() -> list[str]:
    return _features_from_source(_OVERRIDES_SOURCE.read_text(encoding="utf-8"))


def test_feature_extraction_handles_compact_entries() -> None:
    source = (
        'const OVERRIDABLE_UI_FEATURES = [{feature: "canvas"}, {feature: "session_overview"}];'
    )
    assert _features_from_source(source) == ["canvas", "session_overview"]


@pytest.mark.parametrize("expression", ["unknown", '"canvas" + "_extra"', "'canvas'"])
def test_feature_extraction_rejects_unmatched_names(expression: str) -> None:
    with pytest.raises(AssertionError, match="static feature name"):
        _features_from_source(
            f"const OVERRIDABLE_UI_FEATURES = [{{feature: {expression}}}]; "
            'const unrelated = {feature: "canvas"};'
        )


def _definition(name: str):
    return next(
        definition for definition in FEATURE_DEFINITIONS if definition.feature.value == name
    )


def test_the_list_is_read_and_has_no_repeats() -> None:
    features = _overridable_features()
    assert "chat_composer_codemirror" in features
    assert len(features) == len(set(features))


@pytest.mark.parametrize("name", _overridable_features())
def test_each_listed_feature_is_visible_to_the_frontend(name: str) -> None:
    definition = _definition(name)
    assert definition.frontend_visible, f"{name} isn't sent to the page"


@pytest.mark.parametrize("name", _overridable_features())
def test_no_server_features_follow_a_listed_feature(name: str) -> None:
    followers = [
        definition.feature.value
        for definition in FEATURE_DEFINITIONS
        if definition.follows == Feature(name)
    ]
    assert not followers, f"{followers} follow {name} on the server, so it can't be overridden"


@pytest.mark.parametrize("name", _overridable_features())
def test_listed_feature_does_not_follow_another_feature(name: str) -> None:
    follows = _definition(name).follows
    assert follows is None, f"{name} follows {follows} on the server, so it can't be overridden"


def _scan_server_readers(
    root: Path, names: list[str], not_gates: set[tuple[str, str, str]]
) -> dict[str, list[str]]:
    patterns = {
        name: (
            re.compile(rf"\bFeature\.{Feature(name).name}\b"),
            re.compile(rf"[\"']{name}[\"']"),
        )
        for name in names
    }
    readers: dict[str, list[str]] = {name: [] for name in patterns}
    for path in sorted(root.rglob("*.py")):
        if path == root / _DEFINITIONS_FILE:
            continue
        relative = path.relative_to(root).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            for name, (enum_reference, quoted) in patterns.items():
                if not (enum_reference.search(line) or quoted.search(line)):
                    continue
                if (name, relative, line.strip()) in not_gates:
                    continue
                readers[name].append(f"{relative}: {line.strip()}")
    return readers


@cache
def _server_readers() -> dict[str, list[str]]:
    return _scan_server_readers(_SERVER_SOURCE, _overridable_features(), _NOT_GATES)


def test_server_reader_scan_detects_references_and_only_exempts_the_route(tmp_path: Path) -> None:
    (tmp_path / "enum_reference.py").write_text("enabled = Feature.CANVAS\n", encoding="utf-8")
    (tmp_path / "quoted_reference.py").write_text('flag = "canvas"\n"canvas",\n', encoding="utf-8")
    server = tmp_path / "server"
    server.mkdir()
    (server / "app.py").write_text('routes = [\n    "canvas",\n]\n', encoding="utf-8")
    (server / "feature_flags.py").write_text("feature = Feature.CANVAS\n", encoding="utf-8")

    readers = _scan_server_readers(
        tmp_path, ["canvas", "session_overview"], {("canvas", "server/app.py", '"canvas",')}
    )
    assert readers == {
        "canvas": [
            "enum_reference.py: enabled = Feature.CANVAS",
            'quoted_reference.py: flag = "canvas"',
            'quoted_reference.py: "canvas",',
        ],
        "session_overview": [],
    }


@pytest.mark.parametrize("name", _overridable_features())
def test_no_server_code_reads_a_listed_feature(name: str) -> None:
    readers = _server_readers()[name]
    assert readers == [], f"server code reads {name}, so it can't be overridden: {readers}"
