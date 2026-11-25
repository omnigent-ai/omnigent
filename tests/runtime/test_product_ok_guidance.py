"""Agents ask every product approver for product-ok, the ticket's requester included."""

from pathlib import Path

from omnigent.runtime.project_ticket_instructions import PROJECT_TICKET_WORK_INSTRUCTION
from omnigent.tools.builtins.project_tickets import TICKET_TOOLS

SKILL = Path(__file__).parents[2] / "omnigent/resources/skills/orchestrator/SKILL.md"


def test_every_text_an_agent_reads_includes_the_requester_for_product_ok_only():
    texts = {
        "ticket guidance": PROJECT_TICKET_WORK_INSTRUCTION,
        "sys_ticket_get": TICKET_TOOLS["sys_ticket_get"].description(),
        "orchestrator skill": " ".join(SKILL.read_text().split()),
    }
    for where, text in texts.items():
        assert "requester included" in text, where
        assert "only risk-ok excludes the requester" in text.lower().replace("`", ""), where
