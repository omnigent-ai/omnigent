// Placeholder content for the harness details page where there's no backend
// yet: Discover catalogs, MCP tool lists, skill contents, and every action.
// WIP behind the `harnesses` release feature.

import { toast } from "sonner";

export type CatalogKind = "mcps" | "skills" | "plugins";

export interface DiscoverItem {
  name: string;
  description: string;
}

/** Stand-in for actions with no backend yet (install, remove, connect, …). */
export function notAvailableYet() {
  toast("Not available yet");
}

/** Sample tool names for an MCP server; the host doesn't report its tools. */
export function mockTools(server: string): string[] {
  return ["search", "get", "create", "update", "list"].map((verb) => `${verb}_${server}`);
}

export const MOCK_DISCOVER: Record<CatalogKind, DiscoverItem[]> = {
  mcps: [
    { name: "notion", description: "Search and edit pages and databases." },
    { name: "jira", description: "Read and update issues and sprints." },
    { name: "google-drive", description: "Find and read documents and sheets." },
  ],
  skills: [
    { name: "accessibility-audit", description: "Check UI against WCAG guidelines." },
    { name: "incident-response", description: "Guide on-call through triage and mitigation." },
    { name: "query-optimization", description: "Rewrite slow queries and add indexes." },
  ],
  plugins: [
    { name: "security-toolkit", description: "Scanners, secrets checks, and policy gates." },
    { name: "observability-suite", description: "Logs, metrics, and tracing helpers." },
    { name: "release-manager", description: "Versioning, changelogs, and release gates." },
  ],
};

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
