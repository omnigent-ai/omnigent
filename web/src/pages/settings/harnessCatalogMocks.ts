// Placeholder content for the harness details page where there's no backend
// yet: MCP tool lists and skill contents. WIP behind the `harnesses` release
// feature.

export type CatalogKind = "mcps" | "skills" | "plugins";

/** Sample tool names for an MCP server; the host doesn't report its tools. */
export function mockTools(server: string): string[] {
  return ["search", "get", "create", "update", "list"].map((verb) => `${verb}_${server}`);
}

/** Sample SKILL.md body; the host doesn't report skill contents. */
export const MOCK_SKILL_CONTENT = `## Overview

Summarize recent work in three sections: wins, blockers, and next steps.

## When to use this skill

Use this whenever someone asks for a status update, a progress summary, or a
recap of what shipped.

## Instructions

1. Gather the relevant inputs (merged PRs, closed tickets, meeting notes).
2. Group the work into Wins, Blockers, and Next steps.
3. Lead each Win with the outcome and its impact.
4. For each Blocker, state what is blocked and what would unblock it.

## Output format

- **Wins** — 3–5 bullets, outcome-first.
- **Blockers** — 1–3 bullets, each with an ask.
- **Next steps** — 3–5 bullets, each with an owner.
`;
