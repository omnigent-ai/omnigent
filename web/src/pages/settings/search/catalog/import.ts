import type { ImportSourceSelector } from "@/lib/sessionsApi";

import { settingsEntry } from "./types";

/** Harnesses the import endpoint accepts; "all" reads every supported one on the host at once. */
export const IMPORT_SOURCES: readonly { value: ImportSourceSelector; label: string }[] = [
  { value: "all", label: "All harnesses" },
  { value: "claude", label: "Claude Code" },
  { value: "codex", label: "Codex" },
  { value: "opencode", label: "OpenCode" },
  { value: "pi", label: "Pi" },
  { value: "qwen", label: "Qwen" },
  { value: "kiro", label: "Kiro" },
  { value: "kimi", label: "Kimi" },
];

/** How many recent sessions an import can read. */
export const IMPORT_RECENT_LIMITS = [25, 50, 100, 200, 500, 1000] as const;

export const importRecentLimitLabel = (limit: number) => `Last ${limit}`;

export const IMPORT_MACHINE_ENTRY = settingsEntry({
  id: "import-machine",
  section: "import",
  label: "Machine",
  description: "The online machine whose harness transcripts are read and imported.",
  keywords: ["import", "machine", "host", "computer", "laptop", "transcripts"],
  parts: { machine: "Machine" },
  // The online machines, by name.
  overlays: { machine: { runtimeChoices: {} } },
  focus: "machine",
});

export const IMPORT_MODE_ENTRY = settingsEntry({
  id: "import-mode",
  section: "import",
  label: "What to import",
  description: "Import the most recent sessions, or one session by its ID.",
  keywords: ["import", "recent", "session id", "one session", "migrate"],
  parts: { mode: "What to import" },
  partChoices: { mode: ["Recent sessions", "Session by ID"] },
  focus: "mode",
});

export const IMPORT_HARNESS_ENTRY = settingsEntry({
  id: "import-harness",
  section: "import",
  label: "Harness",
  description: "The harness whose sessions are imported, or all of them for recent sessions.",
  keywords: ["import", "harness", "agent", "transcripts"],
  parts: { harness: "Harness" },
  partChoices: { harness: IMPORT_SOURCES.map((source) => source.label) },
  focus: "harness",
});

export const IMPORT_RECENT_COUNT_ENTRY = settingsEntry({
  id: "import-recent-count",
  section: "import",
  label: "How many recent sessions",
  description:
    "Recent sessions you opened in the harness. Sub-agent and automation runs are skipped; prior imports are counted separately and skipped.",
  keywords: ["import", "recent", "how many", "limit", "count", "last"],
  choices: IMPORT_RECENT_LIMITS.map(importRecentLimitLabel),
  // Only importing recent sessions asks how many; otherwise the mode is what to change.
  revealedBy: "import-mode",
});

export const IMPORT_SESSION_ID_ENTRY = settingsEntry({
  id: "import-session-id",
  section: "import",
  label: "Session ID",
  description: "The harness's own ID of the one session to import.",
  keywords: ["import", "session id", "one session", "transcript"],
  parts: { sessionId: "Session ID" },
  focus: "sessionId",
  // Only importing one session asks for its ID.
  revealedBy: "import-mode",
});

export const IMPORT_RUN_ENTRY = settingsEntry({
  id: "import-run",
  section: "import",
  label: "Import",
  description:
    "Import the chosen sessions into this server. Sessions already imported are skipped, and the result shows what failed.",
  keywords: ["import", "start", "migrate", "transcripts", "retry", "failed"],
  parts: {
    import: "Import",
    viewSessions: "View sessions",
    failures: "View failed sessions",
    retry: "Retry failed",
  },
  // The failures disclosure counts them; only its static name is searchable.
  nameTemplates: { failures: { before: "View ", after: " failed sessions" } },
  presence: {
    viewSessions: { states: ["imported"] },
    failures: { states: ["some failed"] },
    retry: { states: ["some failed"] },
  },
  focus: "import",
});

export const IMPORT_SETTINGS = [
  IMPORT_MACHINE_ENTRY,
  IMPORT_MODE_ENTRY,
  IMPORT_HARNESS_ENTRY,
  IMPORT_RECENT_COUNT_ENTRY,
  IMPORT_SESSION_ID_ENTRY,
  IMPORT_RUN_ENTRY,
] as const;
