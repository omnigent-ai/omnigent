import {
  type InventoryHarness,
  INVENTORY_HARNESS_IDS,
  INVENTORY_HARNESSES,
} from "@/hooks/useHarnessInventory";
import { HARNESS_CREDENTIAL_COPY } from "@/lib/harnessSetup";
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
 * The Harnesses page's header: its search, its All/Configured filter and the machine it shows.
 * The machine's menu lists the hosts by name, so its options are runtime choices.
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
    machine: "Machine",
  },
  partChoices: { filter: ["All", "Configured"] },
  // Named by the host it shows, or "Select a host" when none is selected.
  nameTemplates: { machine: { before: "Machine: " } },
  // The picker shows only once a machine is connected.
  presence: { machine: { states: ["has hosts"] } },
  overlays: { machine: { runtimeChoices: {} } },
  focus: "search",
});

/** The credential form's controls, shown in the fix dialog's credentials step. */
const CREDENTIAL_FORM_PARTS = {
  adoptCredential: "Use it",
  apiKey: "API key",
  saveKey: "Save",
  gatewayUrl: "Gateway base URL",
  gatewayKey: "Gateway API key",
  saveGateway: "Save",
} as const;

/** The fix dialog's controls, by part. Each is shown inside the dialog that one of its openers opens. */
export const FIX_DIALOG_PARTS = {
  ...CREDENTIAL_FORM_PARTS,
  installAction: "Install",
  authenticate: "Authenticate",
  checkAgain: "Check again",
  retry: "Try again",
  credentialChoice: "Credential",
  useCredentialForm: "Use an API key or gateway URL instead",
  openSignIn: "Open sign-in page",
  copySignInLink: "Copy sign-in link",
  copyCode: "Copy one-time code",
  copyLoginCommand: "Copy login command",
  copyOmniSetup: "Copy omni setup",
  copyTmuxInstall: "Copy brew install tmux",
  switchConfirm: "Sign out and switch",
  accountHint: "Account to sign in with",
} as const;

/** The credential choice's two cards, named by their labels. */
const FIX_DIALOG_PART_CHOICES = {
  credentialChoice: Object.values(HARNESS_CREDENTIAL_COPY).map((copy) => copy.label),
};

/** The controls that open the fix dialog: a card's Set up, a page's fix, and the Credential card's action. */
export type FixOpener = "setUp" | "fixAction" | "credentialAction";

const FIX_DIALOG_ALTERNATIVES = {
  installAction: { update: "Update" },
  authenticate: { install: "Install", checking: "Checking…" },
  checkAgain: { checking: "Checking…" },
} as const;

/** The dialog's Cancel and Close only dismiss it, so they name it rather than being parts. */
const FIX_DIALOG_OVERLAY = { dialogActions: ["Cancel", "Close"], captured: true } as const;

/** Each fix dialog part is shown only inside the dialog `opener` opens. */
function fixDialogPresence<const Opener extends FixOpener>(opener: Opener) {
  return Object.fromEntries(
    Object.keys(FIX_DIALOG_PARTS).map((part) => [part, { overlay: opener }]),
  ) as { readonly [Part in keyof typeof FIX_DIALOG_PARTS]: { readonly overlay: Opener } };
}

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
        ...FIX_DIALOG_PARTS,
      },
      nameAlternatives: FIX_DIALOG_ALTERNATIVES,
      partChoices: FIX_DIALOG_PART_CHOICES,
      // A card opens its page only when the harness is ready or can be fixed from here; its
      // gear shows once it has settings to open; Set up only for a harness that can be installed.
      presence: {
        open: { states: ["opens its page"] },
        settings: { states: ["has settings"] },
        setUp: { states: ["can be installed"] },
        ...fixDialogPresence("setUp"),
      },
      overlays: { setUp: FIX_DIALOG_OVERLAY },
    });
  },
});

/** The fix a harness page's setup warning offers, by its name's key: "Set up" unless one of these. */
export type HarnessFixActionName = "install" | "update" | "reconfigure" | "tmux";

/** The Credential card's button by key: "Reconfigure" unless one of these. */
export type HarnessCredentialActionName = "switch" | "signInAgain";

/** A harness's own page: the setup warning's fix, and its title row. */
export type HarnessDetailEntry = SettingsEntry & {
  readonly parts: Readonly<Record<"back" | "fixAction", string>>;
  readonly nameAlternatives: {
    readonly fixAction: Readonly<Record<HarnessFixActionName, string>>;
  };
};

/** A harness page that lists its host's inventory, which also has the tab list. */
export type InventoryHarnessDetailEntry = HarnessDetailEntry & {
  readonly parts: Readonly<Record<"back" | "fixAction" | "tab", string>>;
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

/** A harness's plugin page, reached from its Plugins tab: its own Skills and MCPs tabs. */
export type HarnessPluginEntry = SettingsEntry & {
  readonly parts: Readonly<Record<"back" | "tab", string>>;
};

/** A harness's skill page, reached from its Skills tab or from a plugin: the skill's document. */
export type HarnessSkillEntry = SettingsEntry;

/** The entries of one inventory harness's pages; only the families with skill contents have `skill`. */
export interface InventoryPageEntries {
  readonly family: InventoryHarness;
  readonly harness: InventoryPageHarnessId;
  readonly plugin: HarnessPluginEntry;
  readonly skill?: HarnessSkillEntry;
  readonly mcp: SettingsEntry;
}

/**
 * The inventory a harness's page lists, with its entries. A harness without an inventory has
 * none of these pages.
 */
export function harnessInventoryPage(harness: NativeHarnessId): InventoryPageEntries | undefined {
  const family = inventoryFamilyOf(harness);
  if (family === undefined || !isInventoryPageHarness(harness)) return undefined;
  return {
    family,
    harness,
    plugin: HARNESS_PLUGIN_ENTRIES.entry(harness),
    skill: hasSkillPage(family) ? HARNESS_SKILL_ENTRIES.entry(harness) : undefined,
    mcp: MCP_SERVER_LIST_ENTRIES.entry(harness),
  };
}

const INVENTORY_KINDS = ["MCP servers", "Skills", "Plugins"] as const;
// A list whose rows each open a page, named by the row's own data, so none of it is searchable.
const OPENS_BY_NAME = { rowActions: { open: { dataName: true } } } as const;

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
      parts: { back: "Back to Harnesses", fixAction: "Set up", ...FIX_DIALOG_PARTS },
      partChoices: FIX_DIALOG_PART_CHOICES,
      nameAlternatives: {
        fixAction: {
          install: "Install",
          update: "Update",
          reconfigure: "Reconfigure",
          tmux: "Set up tmux",
        },
        ...FIX_DIALOG_ALTERNATIVES,
      },
      presence: { fixAction: { states: ["needs setup"] }, ...fixDialogPresence("fixAction") },
      overlays: { fixAction: FIX_DIALOG_OVERLAY },
    };
    if (inventoryFamilyOf(harness) === undefined) return settingsEntry(page);
    return settingsEntry({
      ...page,
      parts: { ...page.parts, tab: `${name} view` },
      partChoices: { ...page.partChoices, tab: [...INVENTORY_KINDS, "Settings"] },
      // While the inventory loads, a tab shows its plain name; once loaded, its count.
      choiceTemplates: {
        tab: Object.fromEntries(
          INVENTORY_KINDS.map((kind) => [kind, { before: `${kind} · `, count: true }]),
        ),
      },
      presence: { ...page.presence, tab: { states: ["lists its inventory"] } },
      // Each plugin and skill row opens its own page.
      regions: { plugins: OPENS_BY_NAME, skills: OPENS_BY_NAME },
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
      parts: {
        credentialAction: "Reconfigure",
        ...FIX_DIALOG_PARTS,
      },
      partChoices: FIX_DIALOG_PART_CHOICES,
      nameAlternatives: {
        credentialAction: { switch: "Switch account", signInAgain: "Sign in again" },
        ...FIX_DIALOG_ALTERNATIVES,
      },
      presence: {
        credentialAction: { states: ["has a credential action"] },
        ...fixDialogPresence("credentialAction"),
      },
      overlays: { credentialAction: FIX_DIALOG_OVERLAY },
    }),
});

/** The families whose skills open a page of their own, which also show their contents. */
/** The skill-page harnesses by native id, which their skill entries are built for. */
export const SKILL_PAGE_HARNESSES = [
  "claude-native",
  "codex-native",
  "cursor-native",
] as const satisfies readonly NativeHarnessId[];

export type SkillPageHarnessId = (typeof SKILL_PAGE_HARNESSES)[number];

export function isSkillPageHarness(harness: string): harness is SkillPageHarnessId {
  return (SKILL_PAGE_HARNESSES as readonly string[]).includes(harness);
}

/** The harnesses whose page lists their inventory, by native id; their entries are built for these. */
export const INVENTORY_PAGE_HARNESSES = [
  "claude-native",
  "codex-native",
  "cursor-native",
  "pi-native",
] as const satisfies readonly NativeHarnessId[];

export type InventoryPageHarnessId = (typeof INVENTORY_PAGE_HARNESSES)[number];

/** An MCP server list's entry, whose switch is its `enabled` part; only Claude's and Codex's lists have one. */
export type McpServerListEntry = SettingsEntry & {
  readonly parts: Readonly<Record<"enabled", string>>;
};

export function isInventoryPageHarness(harness: string): harness is InventoryPageHarnessId {
  return (INVENTORY_PAGE_HARNESSES as readonly string[]).includes(harness);
}

export const SKILL_PAGE_FAMILIES = [
  "claude",
  "codex",
  "cursor",
] as const satisfies readonly InventoryHarness[];

function hasSkillPage(family: InventoryHarness): boolean {
  return (SKILL_PAGE_FAMILIES as readonly InventoryHarness[]).includes(family);
}

const INVENTORY_NATIVE_IDS = NATIVE_HARNESS_IDS.filter(
  (harness) => inventoryFamilyOf(harness) !== undefined,
);
const SKILL_PAGE_NATIVE_IDS = INVENTORY_NATIVE_IDS.filter((harness) =>
  hasSkillPage(inventoryFamilyOf(harness)!),
);

const PLUGIN_KINDS = ["Skills", "MCPs"] as const;

/** Each inventory harness's plugin page, named by its own tabs; a link to it lands on the Plugins tab. */
export const HARNESS_PLUGIN_ENTRIES = settingsEntryFamily({
  key: "harness-plugins",
  members: INVENTORY_NATIVE_IDS,
  build: (harness): HarnessPluginEntry =>
    settingsEntry({
      id: `harness-${harness}-plugin`,
      section: "harnesses",
      harness,
      label: "Plugin",
      description: `A plugin's skills and MCP servers in ${harnessName(harness)}.`,
      keywords: ["plugin", "skills", "mcp"],
      parts: { back: "Back to Plugins", tab: "Plugin view" },
      partChoices: { tab: [...PLUGIN_KINDS] },
      choiceTemplates: {
        tab: Object.fromEntries(
          PLUGIN_KINDS.map((kind) => [kind, { before: `${kind} · `, count: true }]),
        ),
      },
      regions: { skills: OPENS_BY_NAME },
      revealedBy: { entry: `harness-${harness}-detail`, part: "tab", choice: "Plugins" },
    }),
});

/** Each skill-page family's skill page: the skill's own document, whose links are its content. */
export const HARNESS_SKILL_ENTRIES = settingsEntryFamily({
  key: "harness-skills",
  members: SKILL_PAGE_NATIVE_IDS,
  build: (harness): HarnessSkillEntry =>
    settingsEntry({
      id: `harness-${harness}-skill`,
      section: "harnesses",
      harness,
      label: "Skill",
      description: `A skill's own instructions in ${harnessName(harness)}.`,
      keywords: ["skill", "instructions"],
      // Back to Skills, or, opened from a plugin, back to that plugin.
      parts: { back: "Back to Skills" },
      nameTemplates: { back: { before: "Back to " } },
      contentAreas: ["skill"],
      revealedBy: { entry: `harness-${harness}-detail`, part: "tab", choice: "Skills" },
    }),
});

/**
 * Each inventory harness's MCP list on its page. Claude Code's and Codex's turn each server on
 * or off for Omnigent sessions; the other harnesses' only list them.
 */
export const MCP_SERVER_LIST_ENTRIES = settingsEntryFamily({
  key: "mcp-server-lists",
  members: INVENTORY_NATIVE_IDS,
  build: (harness): SettingsEntry => {
    const name = harnessName(harness);
    const family = inventoryFamilyOf(harness)!;
    const list = {
      id: `mcp-servers-${family}`,
      section: "harnesses" as const,
      harness,
      label: `MCP servers for ${name}`,
      regions: { servers: OPENS_BY_NAME, listed: {} },
    };
    if (harness === "claude-native" || harness === "codex-native") {
      return {
        ...list,
        description: `Turn each MCP server on or off for Omnigent sessions in ${name}.`,
        keywords: [
          "mcp",
          "model context protocol",
          "tools",
          "plugins",
          "servers",
          "enable",
          "disable",
        ],
        parts: { enabled: "Enable MCP server" },
        // Named by the server it switches, and the harness it's for.
        nameTemplates: { enabled: { before: "Enable MCP server: ", after: ` for ${family}` } },
        // Only a server whose host reports it on or off has a switch; a plugin's may not.
        presence: { enabled: { states: ["lists a switchable server"] } },
      };
    }
    return {
      ...list,
      description: `The MCP servers ${name} uses on a machine, and their tools.`,
      keywords: ["mcp", "model context protocol", "tools", "servers"],
    };
  },
});

export const HARNESS_SETTINGS: readonly SettingsEntry[] = [
  HARNESSES_GRID_ENTRY,
  ...HARNESS_CARD_ENTRIES.entries,
  ...HARNESS_DETAIL_ENTRIES.entries,
  ...HARNESS_CREDENTIAL_ENTRIES.entries,
  ...HARNESS_PLUGIN_ENTRIES.entries,
  ...HARNESS_SKILL_ENTRIES.entries,
  ...MCP_SERVER_LIST_ENTRIES.entries,
];
