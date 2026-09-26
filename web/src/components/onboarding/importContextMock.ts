// Sample harness setup for previewing the import modal (Storybook + the dev
// `?import-preview` param) until discovery is backed by the host.

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
    { harness: "claude", source: "Databricks AI Gateway" },
    { harness: "codex", source: "Databricks (dbc-a5d4177a-49dc)" },
    { harness: "cursor", source: "Cursor Enterprise" },
  ],
  mcps: [
    { id: "cursor:confluence", name: "confluence", harness: "cursor", toolCount: 9 },
    {
      id: "claude:databricks-v2",
      name: "databricks-v2",
      harness: "claude",
      toolCount: 18,
    },
    { id: "codex:github", name: "github", harness: "codex", toolCount: 4 },
    { id: "cursor:glean", name: "glean", harness: "cursor", toolCount: 7 },
    { id: "cursor:google", name: "google", harness: "cursor", toolCount: 70 },
    { id: "claude:jira", name: "jira", harness: "claude", toolCount: 4 },
    { id: "claude:safe", name: "safe", harness: "claude", toolCount: 5 },
    { id: "cursor:slack", name: "slack", harness: "cursor", toolCount: 5 },
    { id: "claude:web-search", name: "web-search", harness: "claude", toolCount: 1 },
    { id: "codex:web_search", name: "web_search", harness: "codex", toolCount: 1 },
    {
      id: "cursor:plugin:figma:figma",
      name: "plugin:figma:figma",
      harness: "cursor",
      toolCount: 36,
    },
  ],
  skills: [
    ...CLAUDE_SKILLS.map((name) => ({ id: `claude:${name}`, name, harness: "claude" as const })),
    ...CODEX_SKILLS.map((name) => ({ id: `codex:${name}`, name, harness: "codex" as const })),
  ],
  plugins: [
    { id: "claude:frontend-toolkit", name: "frontend-toolkit", harness: "claude", skillCount: 12 },
    { id: "claude:dev-productivity", name: "dev-productivity", harness: "claude", skillCount: 8 },
    { id: "cursor:figma", name: "figma", harness: "cursor", skillCount: 1 },
  ],
};
