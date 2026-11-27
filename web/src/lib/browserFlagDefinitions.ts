// The desktop flags a build lets a browser with no native shell turn on or off for itself. A
// build registers its definitions once at startup, before the first flag read; without them a
// browser overrides nothing.

export interface BrowserFlagDefinition {
  name: string;
  type: "boolean" | "number" | "json";
  /** Whether pages can read the flag at all. */
  rendererVisible: boolean;
  /** Limits a boolean's overrides, as `[false]` makes it Automatic or Off. */
  overrideValues?: readonly boolean[];
  /** False for a flag that only acts through a desktop shell. */
  browser?: boolean;
}

const NO_DEFINITIONS: readonly BrowserFlagDefinition[] = Object.freeze([]);
let registered = NO_DEFINITIONS;
const registrationListeners = new Set<() => void>();

/** Use `definitions` for every browser override from now on, telling who reads overrides. */
export function registerBrowserFlagDefinitions(
  definitions: readonly BrowserFlagDefinition[],
): void {
  if (registered === definitions) return;
  registered = definitions;
  for (const listener of [...registrationListeners]) listener();
}

/** Call `listener` after each new registration; returns the unsubscribe. */
export function onBrowserFlagDefinitionsChange(listener: () => void): () => void {
  registrationListeners.add(listener);
  return () => {
    registrationListeners.delete(listener);
  };
}

/** The definitions registered last, the same array until the next registration. */
export function browserFlagDefinitions(): readonly BrowserFlagDefinition[] {
  return registered;
}

/** Whether a browser may set `flag` to `value`. */
export function browserOverrideAllowed(
  flag: Pick<BrowserFlagDefinition, "overrideValues">,
  value: boolean,
): boolean {
  return flag.overrideValues === undefined || flag.overrideValues.includes(value);
}

/** Forget the registration, for tests that register their own definitions. */
export function resetBrowserFlagDefinitionsForTests(): void {
  registered = NO_DEFINITIONS;
}
