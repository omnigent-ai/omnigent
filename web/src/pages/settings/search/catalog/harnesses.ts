import {
  type InventoryHarness,
  INVENTORY_HARNESS_IDS,
  INVENTORY_HARNESSES,
} from "@/hooks/useHarnessInventory";
import { NATIVE_CODING_AGENTS } from "@/lib/nativeCodingAgents";

import { type SettingsEntry, settingsEntry, settingsEntryFamily } from "./types";

/** A harness the app knows, by its native id, such as `claude-native`. */
export type NativeHarnessId = (typeof NATIVE_CODING_AGENTS)[number]["harness"];

/** The registry's harnesses, in its own order. */
export const NATIVE_HARNESS_IDS: readonly NativeHarnessId[] = NATIVE_CODING_AGENTS.map(
  (spec) => spec.harness,
);

/** A harness's name as the registry gives it, such as "Claude Code". */
export const harnessName = (harness: NativeHarnessId): string =>
  NATIVE_CODING_AGENTS.find((spec) => spec.harness === harness)!.displayName;

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
    const name = harnessName(harness);
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

/** The fix a harness page's setup warning offers, by its name's key: "Set up" unless one of these. */
export type HarnessFixActionName = "install" | "update" | "reconfigure" | "tmux";

/** The Credential card's button by key: "Reconfigure" unless one of these. */
export type HarnessCredentialActionName = "switch" | "signInAgain";

/** A harness's own page: the setup warning's fix, and its title row. */
export type HarnessDetailEntry = SettingsEntry & {
  readonly parts: Readonly<Record<"fixAction", string>>;
  readonly nameAlternatives: {
    readonly fixAction: Readonly<Record<HarnessFixActionName, string>>;
  };
};

/** A harness page that lists its host's inventory, which also has the tab list. */
export type InventoryHarnessDetailEntry = HarnessDetailEntry & {
  readonly parts: Readonly<Record<"fixAction" | "tab", string>>;
};

/** A harness's Credential card, whose button signs in again or switches account. */
export type HarnessCredentialEntry = SettingsEntry & {
  readonly parts: Readonly<Record<"credentialAction", string>>;
  readonly nameAlternatives: {
    readonly credentialAction: Readonly<Record<HarnessCredentialActionName, string>>;
  };
};

function inventoryFamilyOf(harness: NativeHarnessId): InventoryHarness | undefined {
  return INVENTORY_HARNESSES.find((family) => INVENTORY_HARNESS_IDS[family] === harness);
}

/**
 * The inventory a harness's page lists, by its family, with the page's entry, which has the tab
 * list. A harness without an inventory has no such page.
 */
export function harnessInventoryPage(
  harness: NativeHarnessId,
): { family: InventoryHarness; detail: InventoryHarnessDetailEntry } | undefined {
  const family = inventoryFamilyOf(harness);
  if (family === undefined) return undefined;
  // Only the inventory pages' builds add the tab list, so this is the one place that names it.
  return {
    family,
    detail: HARNESS_DETAIL_ENTRIES.entry(harness) as InventoryHarnessDetailEntry,
  };
}

const INVENTORY_KINDS = ["MCP servers", "Skills", "Plugins"] as const;

/**
 * Each harness's own page, named by its title. Its setup warning's button is "Set up", or, by
 * what the host needs, Install, Update, Reconfigure or Set up tmux. A page whose harness has an
 * inventory also has the tabs that list it, and Settings.
 */
export const HARNESS_DETAIL_ENTRIES = settingsEntryFamily({
  key: "harness-details",
  members: NATIVE_HARNESS_IDS,
  build: (harness): HarnessDetailEntry => {
    const name = harnessName(harness);
    const page = {
      id: `harness-${harness}-detail`,
      section: "harnesses" as const,
      harness,
      label: name,
      description: `${name} on a machine: its MCP servers, skills and plugins, and its setup.`,
      keywords: ["harness", "agent", "setup"],
      parts: { fixAction: "Set up" },
      nameAlternatives: {
        fixAction: {
          install: "Install",
          update: "Update",
          reconfigure: "Reconfigure",
          tmux: "Set up tmux",
        },
      },
      presence: { fixAction: { states: ["needs setup"] } },
    };
    if (inventoryFamilyOf(harness) === undefined) return settingsEntry(page);
    return settingsEntry({
      ...page,
      parts: { fixAction: "Set up", tab: `${name} view` },
      partChoices: { tab: [...INVENTORY_KINDS, "Settings"] },
      // While the inventory loads, a tab shows its plain name; once loaded, its count.
      choiceTemplates: {
        tab: Object.fromEntries(
          INVENTORY_KINDS.map((kind) => [kind, { before: `${kind} · `, count: true }]),
        ),
      },
      presence: { ...page.presence, tab: { states: ["lists its inventory"] } },
    }) as InventoryHarnessDetailEntry;
  },
});

/**
 * Each harness's Credential card on its page. Its button is "Reconfigure" when the credential
 * needs fixing, or Switch account or Sign in again where the host can do that.
 */
export const HARNESS_CREDENTIAL_ENTRIES = settingsEntryFamily({
  key: "harness-credentials",
  members: NATIVE_HARNESS_IDS,
  build: (harness): HarnessCredentialEntry =>
    settingsEntry({
      id: `harness-${harness}-credential`,
      section: "harnesses",
      harness,
      label: "Credential",
      description: `How ${harnessName(harness)} signs in on a machine, and changing it.`,
      keywords: ["credential", "sign in", "account", "login", "api key"],
      parts: { credentialAction: "Reconfigure" },
      nameAlternatives: {
        credentialAction: { switch: "Switch account", signInAgain: "Sign in again" },
      },
      presence: { credentialAction: { states: ["has a credential action"] } },
    }),
});

export const HARNESS_SETTINGS: readonly SettingsEntry[] = [
  HARNESSES_GRID_ENTRY,
  ...HARNESS_CARD_ENTRIES.entries,
  ...HARNESS_DETAIL_ENTRIES.entries,
  ...HARNESS_CREDENTIAL_ENTRIES.entries,
];
