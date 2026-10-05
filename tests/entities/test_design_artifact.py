"""Tests for classifying Design page artifact paths."""

from __future__ import annotations

import pytest

from omnigent.entities.design_artifact import design_artifact_kind


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("decks/q3.slides.html", "deck"),
        (".omnigent/design-system/slides/title.slides.html", None),
        (".omnigent/design-system/templates/flow.wireframe.html", None),
        ("/work/app/.omnigent/design-system/slides/a.slides.html", None),
        (".omnigent/design-systems/a.slides.html", "deck"),
        ("design-system/a.slides.html", "deck"),
    ],
)
def test_imported_design_system_is_not_an_artifact(path: str, kind: str | None) -> None:
    """Files copied into ``.omnigent/design-system/`` are never indexed as decks or wireframes."""
    assert design_artifact_kind(path) == kind
