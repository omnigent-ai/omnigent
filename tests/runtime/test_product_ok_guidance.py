"""Agents ask every product approver for product-ok and risk-ok, the requester included."""

from pathlib import Path

from omnigent.runtime.project_ticket_instructions import PROJECT_TICKET_WORK_INSTRUCTION
from omnigent.tools.builtins.project_tickets import TICKET_TOOLS

SKILL = Path(__file__).parents[2] / "omnigent/resources/skills/orchestrator/SKILL.md"


def test_every_text_an_agent_reads_includes_the_requester_for_both_sign_offs():
    texts = {
        "ticket guidance": PROJECT_TICKET_WORK_INSTRUCTION,
        "sys_ticket_get": TICKET_TOOLS["sys_ticket_get"].description(),
        "orchestrator skill": " ".join(SKILL.read_text().split()),
    }
    for where, text in texts.items():
        assert "requester included" in text, where
        assert "risk-ok" in text.replace("`", ""), where
        assert "excludes the requester" not in text, where
        assert "didn't request the ticket" not in text, where
