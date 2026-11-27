import type { FeatureKey, ServerInfo } from "./capabilities";
import { getOmnigentServerIdentity } from "./host";

export interface OverridableUiFeature {
  feature: FeatureKey;
  label: string;
  description: string;
}

export const OVERRIDABLE_UI_FEATURES: readonly OverridableUiFeature[] = [
  {
    feature: "chat_composer_codemirror",
    label: "CodeMirror chat input",
    description: "Write chat messages in a CodeMirror editor that scrolls long drafts.",
  },
  {
    feature: "warm_switch_keep_mounted_ui",
    label: "Keep chat mounted on switch",
    description: "Switch between loaded sidebar sessions without rebuilding the chat view.",
  },
  {
    feature: "quick_pick_palette",
    label: "QuickPick command palette",
    description: "Run ⌘K and the session switcher on the fuzzy-matching QuickPick list.",
  },
  {
    feature: "sidebar_grouping_ui",
    label: "Sidebar grouping",
    description: "Grouping, ordering and row details in the sidebar, and a Shared section.",
  },
  {
    feature: "grid_sessions_display_ui",
    label: "Session grids",
    description: "Drag sessions into a conversation to see up to four live chats at once.",
  },
  {
    feature: "splittable_terminal_view_ui",
    label: "Split terminal view",
    description: "Split the main terminal view into several shell panes.",
  },
  {
    feature: "canvas",
    label: "Canvas",
    description: "A Canvas page with sessions as draggable cards, grouped by project.",
  },
  {
    feature: "session_overview",
    label: "Session cost sidebar",
    description: "Show session cost and token usage in a sidebar beside chat.",
  },
  {
    feature: "project_kanban",
    label: "Project home",
    description:
      "Open a project's home page, with its sessions in status columns. An older server may not support it.",
  },
  {
    feature: "project_repository_creation",
    label: "Projects from a repository",
    description:
      "Create a project by cloning a repository on a host. An older server may not support it.",
  },
  {
    feature: "floating_composer",
    label: "Floating composer",
    description: "Open the floating New Agent panel from the desktop app.",
  },
  {
    feature: "import_review",
    label: "Import review",
    description: "Open the import review automatically when a new host connects.",
  },
];

export type UiFeatureOverrides = Readonly<Partial<Record<FeatureKey, boolean>>>;
const OVERRIDABLE = new Set<string>(OVERRIDABLE_UI_FEATURES.map(({ feature }) => feature));
const STORAGE_PREFIX = "omnigent:ui-feature-overrides:v1:";
// Read once per page load: every change reloads the page.
const cache = new Map<string, UiFeatureOverrides | null>();

function storageKey(): string | null {
  if (typeof window === "undefined") return null;
  const server = getOmnigentServerIdentity();
  return server ? `${STORAGE_PREFIX}${JSON.stringify(server)}` : null;
}

export function canOverrideUiFeatures(): boolean {
  return storageKey() !== null;
}

function parseUiFeatureOverrides(stored: string | null): UiFeatureOverrides {
  const overrides: Partial<Record<FeatureKey, boolean>> = {};
  try {
    const raw: unknown = JSON.parse(stored ?? "null");
    if (raw !== null && typeof raw === "object" && !Array.isArray(raw)) {
      for (const [name, value] of Object.entries(raw)) {
        if (OVERRIDABLE.has(name) && typeof value === "boolean") {
          overrides[name as FeatureKey] = value;
        }
      }
    }
  } catch {
    // Ignore malformed storage.
  }
  return overrides;
}

export function readUiFeatureOverrides(): UiFeatureOverrides {
  const key = storageKey();
  if (key === null) return {};
  if (cache.has(key)) return cache.get(key) ?? {};
  try {
    const overrides = parseUiFeatureOverrides(window.localStorage.getItem(key));
    cache.set(key, overrides);
    return overrides;
  } catch {
    cache.set(key, null);
    return {};
  }
}

export function writeUiFeatureOverride(feature: FeatureKey, value: boolean | null): boolean {
  const key = storageKey();
  if (key === null || !OVERRIDABLE.has(feature)) return false;
  try {
    const previous = parseUiFeatureOverrides(window.localStorage.getItem(key));
    const overrides =
      value === null
        ? Object.fromEntries(Object.entries(previous).filter(([name]) => name !== feature))
        : { ...previous, [feature]: value };
    if (Object.keys(overrides).length === 0) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, JSON.stringify(overrides));
    cache.set(key, overrides);
    return true;
  } catch {
    return false;
  }
}

export function clearUiFeatureOverrides(): boolean {
  const key = storageKey();
  if (key === null) return false;
  try {
    window.localStorage.removeItem(key);
    cache.set(key, {});
    return true;
  } catch {
    return false;
  }
}

export function effectiveQuickPickEnabled(
  info: ServerInfo | "loading",
  overrides: UiFeatureOverrides,
): boolean {
  if (info === "loading" || !info.features || !Object.hasOwn(info.features, "quick_pick_palette"))
    return false;
  return overrides.quick_pick_palette ?? info.features.quick_pick_palette === true;
}

export function canBootstrapQuickPick(info: ServerInfo | "loading"): boolean {
  return (
    info !== "loading" &&
    !!info.features &&
    Object.hasOwn(info.features, "quick_pick_palette") &&
    !effectiveQuickPickEnabled(info, readUiFeatureOverrides()) &&
    canOverrideUiFeatures()
  );
}

export function activeUiFeatureOverrides(info: ServerInfo | "loading") {
  if (info === "loading") return [];
  const overrides = readUiFeatureOverrides();
  if (!effectiveQuickPickEnabled(info, overrides)) return [];
  return OVERRIDABLE_UI_FEATURES.flatMap((entry) => {
    const value = overrides[entry.feature];
    return Object.hasOwn(info.features, entry.feature) && value !== undefined
      ? [{ ...entry, value }]
      : [];
  });
}

export function resetUiFeatureOverridesForTests(): void {
  cache.clear();
}
