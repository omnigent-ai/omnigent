// Sample harness setup for previewing the import modal in Storybook and the
// dev-only `?import-preview` param, without a connected host.

import type { ImportContext } from "./ImportContextModal";

const CLAUDE_SKILLS = [
  "create-kafka-topic",
  "create-system",
  "dashboard-analyzer",
  "db-inspect",
  "db-inspect-dev",
  "debug-ci-failures-job",
  "debug-ci-failures-pr",
  "debug-pipeline",
  "deploy-omnigent-databricks",
  "dev-productivity-survey",
];
const CODEX_SKILLS = ["code-review", "fix-lint", "ship"];

export const MOCK_IMPORT_CONTEXT: ImportContext = {
  credentials: [
    { harness: "claude", source: "Databricks Unity Gateway" },
    { harness: "codex", source: "Databricks (dbc-a5d4177a-49dc)" },
    { harness: "cursor", source: "Signed in" },
  ],
  mcps: [
    { id: "cursor:confluence", name: "confluence", harness: "cursor" },
    { id: "claude:databricks-v2", name: "databricks-v2", harness: "claude" },
    { id: "codex:github", name: "github", harness: "codex" },
    { id: "cursor:glean", name: "glean", harness: "cursor" },
    { id: "cursor:google", name: "google", harness: "cursor" },
    { id: "claude:jira", name: "jira", harness: "claude", detail: "mcp.atlassian.com" },
    { id: "claude:safe", name: "safe", harness: "claude" },
    { id: "cursor:slack", name: "slack", harness: "cursor" },
    { id: "claude:web-search", name: "web-search", harness: "claude" },
    { id: "codex:web_search", name: "web_search", harness: "codex" },
    {
      id: "claude:plugin:figma:figma",
      name: "figma",
      harness: "claude",
      detail: "figma plugin · mcp.figma.com",
    },
  ],
  skills: [
    ...CLAUDE_SKILLS.map((name) => ({ id: `claude:${name}`, name, harness: "claude" as const })),
    ...CODEX_SKILLS.map((name) => ({ id: `codex:${name}`, name, harness: "codex" as const })),
  ],
  plugins: [
    { id: "claude:frontend-toolkit", name: "frontend-toolkit", harness: "claude", skillCount: 12 },
    { id: "claude:dev-productivity", name: "dev-productivity", harness: "claude", skillCount: 8 },
    { id: "claude:figma", name: "figma", harness: "claude", skillCount: 1 },
  ],
};
