// Desktop flags a browser turned on or off for itself, kept in this site's localStorage. Only a
// browser with no native shell reads them; the desktop and mobile apps take their shell's flags.
// Which flags it may set comes from the build's registered definitions.

import {
  type BrowserFlagDefinition,
  browserFlagDefinitions,
  browserOverrideAllowed,
  onBrowserFlagDefinitionsChange,
} from "@/lib/browserFlagDefinitions";

export const BROWSER_FLAG_OVERRIDES_KEY = "omnigent.browserFeatureFlagOverrides.v1";

/** The choices this browser made, by flag name; a flag that isn't listed is Automatic. */
export type BrowserFlagOverrides = Readonly<Record<string, boolean>>;

const NO_OVERRIDES: BrowserFlagOverrides = Object.freeze({});

/** The flags a browser may set: page-visible booleans that act without a desktop shell. */
export function browserFlags(
  definitions: readonly BrowserFlagDefinition[] = browserFlagDefinitions(),
): BrowserFlagDefinition[] {
  return definitions.filter(
    (flag) => flag.rendererVisible && flag.type === "boolean" && flag.browser !== false,
  );
}

let flagsByName: {
  definitions: readonly BrowserFlagDefinition[];
  flags: ReadonlyMap<string, BrowserFlagDefinition>;
} | null = null;

/** The browser-settable definition named `name`, if any, under the current registration. */
function browserFlag(name: string): BrowserFlagDefinition | undefined {
  const definitions = browserFlagDefinitions();
  if (flagsByName?.definitions !== definitions) {
    flagsByName = {
      definitions,
      flags: new Map(browserFlags(definitions).map((flag) => [flag.name, flag])),
    };
  }
  return flagsByName.flags.get(name);
}

/** Stored text as a JSON object; null where it's absent or malformed. */
function parseStored(raw: string | null): Record<string, unknown> | null {
  if (raw === null) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** Whatever storage holds, as JSON; null where it's empty, malformed or refused. */
function readStoredJson(): Record<string, unknown> | null {
  try {
    return parseStored(window.localStorage.getItem(BROWSER_FLAG_OVERRIDES_KEY));
  } catch {
    return null;
  }
}

/** The stored choices a browser may act on: unknown, hidden and malformed entries read as none. */
function overridesFrom(stored: Record<string, unknown> | null): BrowserFlagOverrides {
  if (!stored) return NO_OVERRIDES;
  const overrides: Record<string, boolean> = {};
  for (const [name, value] of Object.entries(stored)) {
    const flag = browserFlag(name);
    if (flag && typeof value === "boolean" && browserOverrideAllowed(flag, value)) {
      overrides[name] = value;
    }
  }
  return Object.keys(overrides).length > 0 ? Object.freeze(overrides) : NO_OVERRIDES;
}

const listeners = new Set<() => void>();
let snapshot: {
  raw: string;
  definitions: readonly BrowserFlagDefinition[];
  overrides: BrowserFlagOverrides;
} | null = null;

function rawKey(stored: Record<string, unknown> | null): string {
  return stored ? JSON.stringify(stored) : "";
}

/** This browser's choices, the same object until they change. */
export function browserFlagOverrides(): BrowserFlagOverrides {
  const stored = readStoredJson();
  const raw = rawKey(stored);
  const definitions = browserFlagDefinitions();
  if (snapshot?.raw !== raw || snapshot.definitions !== definitions) {
    const first = snapshot === null;
    snapshot = { raw, definitions, overrides: overridesFrom(stored) };
    if (!first || Object.keys(snapshot.overrides).length > 0) logOverrides(snapshot.overrides);
  }
  return snapshot.overrides;
}

/** The choice for `name`, or undefined while it's Automatic. */
export function browserFlagOverride(name: string): boolean | undefined {
  const overrides = browserFlagOverrides();
  // Own choices only, so a flag named like `constructor` never reads an inherited function.
  return Object.hasOwn(overrides, name) ? overrides[name] : undefined;
}

/**
 * Set `name` to On or Off in this browser, or back to Automatic with undefined. Leaves every
 * other stored entry alone, including flags this build hides. False where storage refused it.
 */
export function setBrowserFlagOverride(name: string, value: boolean | undefined): boolean {
  const flag = browserFlag(name);
  if (!flag || (value !== undefined && !browserOverrideAllowed(flag, value))) return false;
  try {
    // Read inside the same guard: a refused read must not pass for empty storage and drop the rest.
    const current = parseStored(window.localStorage.getItem(BROWSER_FLAG_OVERRIDES_KEY));
    const next = Object.fromEntries(
      Object.entries(current ?? {}).filter(([stored]) => stored !== name),
    );
    if (value !== undefined) next[name] = value;
    if (Object.keys(next).length === 0) window.localStorage.removeItem(BROWSER_FLAG_OVERRIDES_KEY);
    else window.localStorage.setItem(BROWSER_FLAG_OVERRIDES_KEY, JSON.stringify(next));
  } catch {
    return false;
  }
  // The writing tab never hears its own `storage` event.
  notify();
  return true;
}

function notify(): void {
  browserFlagOverrides();
  for (const listener of [...listeners]) listener();
}

/** Another tab of this site changed the choices, or cleared this site's storage. */
function onStorage(event: StorageEvent): void {
  if (event.key === BROWSER_FLAG_OVERRIDES_KEY || event.key === null) notify();
}

// Ends the registration watch while anyone listens; null otherwise.
let stopRegistrationWatch: (() => void) | null = null;

/**
 * Hear every change to this browser's choices, from this tab or another, and each new
 * registration of definitions, which changes which stored choices apply.
 */
export function subscribeBrowserFlagOverrides(listener: () => void): () => void {
  if (listeners.size === 0) {
    window.addEventListener("storage", onStorage);
    stopRegistrationWatch = onBrowserFlagDefinitionsChange(notify);
  }
  listeners.add(listener);
  return () => {
    if (!listeners.delete(listener) || listeners.size > 0) return;
    window.removeEventListener("storage", onStorage);
    stopRegistrationWatch?.();
    stopRegistrationWatch = null;
  };
}

/** Name the choices in effect, so a report from an overridden page says what changed it. */
function logOverrides(overrides: BrowserFlagOverrides): void {
  const entries = Object.entries(overrides);
  const named = entries.map(([name, value]) => `${name}=${value ? "on" : "off"}`).join(", ");
  console.info(`[featureFlags] browser overrides: ${entries.length > 0 ? named : "none"}`);
}

/** Forget the cached read, for tests that rewrite storage underneath the store. */
export function resetBrowserFlagOverridesForTests(): void {
  snapshot = null;
}
