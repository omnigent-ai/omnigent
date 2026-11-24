from __future__ import annotations

from omnigent.entities.mandate_sections import (
    apply_proposal,
    proposal_is_stale,
    proposal_target,
    section_spans,
    text_hash,
)
from omnigent.entities.project import MandateProposal

MANDATE = """# Mandate

Intro.

## Stages

- Pending
- Verified

### Verified details

Isolated server.

```
## Stages
```

## Git

Trailer.
"""


def _proposal(mode: str, text: str, section: str | None = None) -> MandateProposal:
    draft = MandateProposal(id="p", text=text, created_at=0, mode=mode, section=section)
    target = proposal_target(MANDATE, draft)
    return MandateProposal(
        id="p", text=text, created_at=0, mode=mode, section=section, base_hash=text_hash(target or "")
    )


def test_a_section_runs_to_the_next_heading_at_its_level_and_skips_fenced_headings() -> None:
    spans = section_spans(MANDATE, "  stages ")
    assert len(spans) == 1
    start, end = spans[0]
    section = MANDATE[start:end]
    assert section.startswith("## Stages") and "### Verified details" in section
    assert "## Git" not in section
    assert section_spans(MANDATE, "Missing") == []


def test_replacing_and_deleting_a_section_keep_the_rest() -> None:
    replaced = apply_proposal(MANDATE, _proposal("replace_section", "## Stages\n\n- Done", "Stages"))
    assert "## Stages\n\n- Done\n\n## Git" in replaced
    assert "Verified details" not in replaced and replaced.startswith("# Mandate")
    deleted = apply_proposal(MANDATE, _proposal("replace_section", "", "Git"))
    assert "## Git" not in deleted and deleted.rstrip().endswith("```")
    assert apply_proposal(MANDATE, _proposal("replace_all", " New. ")) == "New."
    assert apply_proposal(MANDATE, _proposal("append", "Extra.")).endswith("Trailer.\n\nExtra.")


def test_a_replacement_goes_stale_only_when_its_own_text_changes() -> None:
    section = _proposal("replace_section", "## Git\n\nNo trailer.", "Git")
    whole = _proposal("replace_all", "New.")
    elsewhere = MANDATE.replace("Intro.", "Intro, edited.")
    assert not proposal_is_stale(MANDATE, section)
    assert not proposal_is_stale(elsewhere, section)
    assert proposal_is_stale(elsewhere, whole)
    assert proposal_is_stale(MANDATE.replace("Trailer.", "Changed."), section)
    assert proposal_is_stale(MANDATE.replace("## Git", "## Publishing"), section)
    assert proposal_target(MANDATE.replace("## Git", "## Publishing"), section) is None
    assert not proposal_is_stale(None, _proposal("append", "Extra."))
