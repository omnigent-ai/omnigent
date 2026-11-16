import { NATIVE_CODING_AGENTS } from "@/lib/nativeCodingAgents";

import { type SettingsEntry, settingsEntry, settingsEntryFamily } from "./types";

/** A harness the app knows, by its native id, such as `claude-native`. */
export type NativeHarnessId = (typeof NATIVE_CODING_AGENTS)[number]["harness"];

/** The registry's harnesses, in its own order. */
export const NATIVE_HARNESS_IDS: readonly NativeHarnessId[] = NATIVE_CODING_AGENTS.map(
  (spec) => spec.harness,
);

// One line per harness, keyed by native id. The server's harness list carries no descriptions.
export const HARNESS_DESCRIPTIONS: Readonly<Record<string, string>> = {
  "claude-native": "Anthropic’s agent for coding and development workflows.",
  "codex-native": "OpenAI’s coding agent for building features and fixing bugs.",
  "opencode-native": "An open-source coding agent for terminal, IDE, and desktop.",
  "cursor-native": "An AI editor with agents for navigating and editing code.",
  "pi-native": "A minimal, extensible terminal coding agent.",
  "devin-native": "Cognition’s autonomous coding agent.",
  "antigravity-native": "Google’s agent platform for software development.",
  "kiro-native": "An agentic IDE for spec-driven development.",
  "qwen-native": "An open-source terminal coding agent powered by Qwen.",
  "goose-native": "An open-source local agent for coding and automation.",
  "kimi-native": "A terminal coding agent for editing code and running commands.",
  "hermes-native": "An agent that learns reusable skills from experience.",
};

/**
 * The Harnesses page's header: its search, its All/Configured filter. The machine picker isn't
 * a part yet, so it keeps its own name.
 */
export const HARNESSES_GRID_ENTRY = settingsEntry({
  id: "harnesses",
  section: "harnesses",
  label: "Harnesses",
  description: "Which agent harnesses are set up on a machine, and setting them up.",
  keywords: ["harness", "agent", "machine", "host", "install", "setup"],
  parts: {
    search: "Search agent harnesses",
    filter: "Harness filter",
  },
  partChoices: { filter: ["All", "Configured"] },
  focus: "search",
});

/**
 * A harness's card on the Harnesses page. Its link is named by the card's content, which shows
 * the harness's status, so the link carries only its id, not a name of its own.
 */
export const HARNESS_CARD_ENTRIES = settingsEntryFamily({
  key: "harness-cards",
  members: NATIVE_HARNESS_IDS,
  build: (harness) => {
    const name = NATIVE_CODING_AGENTS.find((spec) => spec.harness === harness)!.displayName;
    return settingsEntry({
      id: `harness-${harness}`,
      section: "harnesses",
      label: name,
      description: HARNESS_DESCRIPTIONS[harness] ?? "",
      keywords: ["harness", "agent"],
      parts: {
        open: name,
        settings: `${name} settings`,
        setUp: `Set up ${name}`,
      },
      // A card opens its page only when the harness is ready or can be fixed from here; its
      // gear shows once it has settings to open; Set up only for a harness that can be installed.
      presence: {
        open: { states: ["opens its page"] },
        settings: { states: ["has settings"] },
        setUp: { states: ["can be installed"] },
      },
    });
  },
});

export const HARNESS_SETTINGS: readonly SettingsEntry[] = [
  HARNESSES_GRID_ENTRY,
  ...HARNESS_CARD_ENTRIES.entries,
];
