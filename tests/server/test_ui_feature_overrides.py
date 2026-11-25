"""UI feature overrides must not alter server-gated behavior."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnigent.server.feature_flags import FEATURE_DEFINITIONS, Feature

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_OVERRIDES_SOURCE = _REPO_ROOT / "web" / "src" / "lib" / "uiFeatureOverrides.ts"
_SERVER_SOURCE = _REPO_ROOT / "omnigent"
_DEFINITIONS_FILE = _SERVER_SOURCE / "server" / "feature_flags.py"
# Mentions that name a feature without gating anything: (feature, file, line text).
_NOT_GATES = {
    # The SPA's route prefixes, so `/canvas` falls back to index.html.
    ("canvas", "server/app.py", '"canvas",'),
}


def _overridable_features() -> list[str]:
    source = _OVERRIDES_SOURCE.read_text(encoding="utf-8")
    return re.findall(r'^\s+feature: "([a-z0-9_]+)",$', source, re.MULTILINE)


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
def test_no_server_code_reads_a_listed_feature(name: str) -> None:
    enum_reference = re.compile(rf"\bFeature\.{Feature(name).name}\b")
    quoted = re.compile(rf"[\"']{name}[\"']")
    readers = []
    for path in sorted(_SERVER_SOURCE.rglob("*.py")):
        if path == _DEFINITIONS_FILE:
            continue
        relative = path.relative_to(_SERVER_SOURCE).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not (enum_reference.search(line) or quoted.search(line)):
                continue
            if (name, relative, line.strip()) in _NOT_GATES:
                continue
            readers.append(f"{relative}: {line.strip()}")
    assert readers == [], f"server code reads {name}, so it can't be overridden: {readers}"
