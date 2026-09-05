export type UserPreferenceNamespace =
  | "keyboard_shortcuts"
  | "mobile_assistant"
  | "session_navigation"
  | "context_indicator"
  | "usage_context"
  | "agent_badges";

export interface UserPreferencesEnvelope {
  version: 1;
  settings: Partial<Record<UserPreferenceNamespace, unknown>>;
}

type PreferenceFetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const CATEGORY_CONFIG: Record<UserPreferenceNamespace, { storageKey: string; eventName: string }> =
  {
    keyboard_shortcuts: {
      storageKey: "omnigent:keyboard-shortcut-preferences",
      eventName: "omnigent:keyboard-shortcuts-changed",
    },
    mobile_assistant: {
      storageKey: "omnigent:mobile-assistant-preferences",
      eventName: "omnigent:mobile-assistant-changed",
    },
    session_navigation: {
      storageKey: "omnigent:session-navigation",
      eventName: "omnigent:session-navigation-changed",
    },
    context_indicator: {
      storageKey: "omnigent:context-indicator-mode",
      eventName: "omnigent:context-indicator-mode-changed",
    },
    usage_context: {
      storageKey: "omnigent:usage-context-preferences",
      eventName: "omnigent:usage-context-preferences-changed",
    },
    agent_badges: {
      storageKey: "omnigent:agent-badge-preferences",
      eventName: "omnigent:agent-badge-preferences-changed",
    },
  };

const USER_PREFERENCES_SCOPE_KEY = "omnigent:user-preferences-owner";
const USER_PREFERENCES_DIRTY_KEY = "omnigent:user-preferences-dirty";
const USER_PREFERENCES_DIRTY_VALUES_KEY = "omnigent:user-preferences-dirty-values";
const MOBILE_ASSISTANT_DEVICE_STORAGE_KEY = "omnigent:mobile-assistant-device-state";

let preferenceFetcher: PreferenceFetcher | null = null;
let serverSupportsPreferences = false;
let applyingServerPreferences = false;
let activeOwnerId: string | null = null;
let activeServerId = "__default__";
let activeConnectionId = "__default__";
let syncGeneration = 0;
let preferenceConnectionIsCurrent = () => true;

function isCurrentPreferenceGeneration(generation: number): boolean {
  return generation === syncGeneration && preferenceConnectionIsCurrent();
}
let refreshListenersInstalled = false;
let lastFocusRefreshAt = 0;
const pendingTimers = new Map<UserPreferenceNamespace, number>();
const lastAcknowledged = new Map<UserPreferenceNamespace, string>();
interface PendingPatch {
  syncValue: unknown | null;
  serialized: string;
  delayMs: number;
  attempt: number;
}
const patchesAfterFlight = new Map<UserPreferenceNamespace, PendingPatch>();
const inFlightPatches = new Map<UserPreferenceNamespace, number>();

function dirtyOwnerKey(): string {
  return `${activeServerId}:${activeOwnerId ?? "__local__"}`;
}

function readDirtyNamespaces(): Set<UserPreferenceNamespace> {
  const parsed = safeParse(window.localStorage.getItem(USER_PREFERENCES_DIRTY_KEY));
  if (!parsed || typeof parsed !== "object") return new Set();
  const values = (parsed as Record<string, unknown>)[dirtyOwnerKey()];
  if (!Array.isArray(values)) return new Set();
  return new Set(
    values.filter((value): value is UserPreferenceNamespace => value in CATEGORY_CONFIG),
  );
}

function writeDirtyNamespaces(values: Set<UserPreferenceNamespace>): void {
  const parsed = safeParse(window.localStorage.getItem(USER_PREFERENCES_DIRTY_KEY));
  const all =
    parsed && typeof parsed === "object" ? { ...(parsed as Record<string, unknown>) } : {};
  if (values.size === 0) Reflect.deleteProperty(all, dirtyOwnerKey());
  else all[dirtyOwnerKey()] = [...values];
  if (Object.keys(all).length === 0) window.localStorage.removeItem(USER_PREFERENCES_DIRTY_KEY);
  else window.localStorage.setItem(USER_PREFERENCES_DIRTY_KEY, JSON.stringify(all));
}

function readDirtyValue(namespace: UserPreferenceNamespace): { found: boolean; value: unknown } {
  const parsed = safeParse(window.localStorage.getItem(USER_PREFERENCES_DIRTY_VALUES_KEY));
  if (!parsed || typeof parsed !== "object") return { found: false, value: null };
  const ownerValues = (parsed as Record<string, unknown>)[dirtyOwnerKey()];
  if (!ownerValues || typeof ownerValues !== "object" || Array.isArray(ownerValues)) {
    return { found: false, value: null };
  }
  if (!Object.hasOwn(ownerValues, namespace)) {
    return { found: false, value: null };
  }
  return { found: true, value: (ownerValues as Record<string, unknown>)[namespace] };
}

function writeDirtyValue(namespace: UserPreferenceNamespace, value: unknown, remove = false): void {
  const parsed = safeParse(window.localStorage.getItem(USER_PREFERENCES_DIRTY_VALUES_KEY));
  const all =
    parsed && typeof parsed === "object" ? { ...(parsed as Record<string, unknown>) } : {};
  const current = all[dirtyOwnerKey()];
  const ownerValues =
    current && typeof current === "object" && !Array.isArray(current)
      ? { ...(current as Record<string, unknown>) }
      : {};
  if (remove) Reflect.deleteProperty(ownerValues, namespace);
  else ownerValues[namespace] = value;
  if (Object.keys(ownerValues).length === 0) Reflect.deleteProperty(all, dirtyOwnerKey());
  else all[dirtyOwnerKey()] = ownerValues;
  if (Object.keys(all).length === 0)
    window.localStorage.removeItem(USER_PREFERENCES_DIRTY_VALUES_KEY);
  else window.localStorage.setItem(USER_PREFERENCES_DIRTY_VALUES_KEY, JSON.stringify(all));
}

function markDirty(namespace: UserPreferenceNamespace, value: unknown): void {
  const dirty = readDirtyNamespaces();
  dirty.add(namespace);
  writeDirtyNamespaces(dirty);
  writeDirtyValue(namespace, value);
}

function clearDirty(namespace: UserPreferenceNamespace): void {
  const dirty = readDirtyNamespaces();
  dirty.delete(namespace);
  writeDirtyNamespaces(dirty);
  writeDirtyValue(namespace, null, true);
}

function safeParse(raw: string | null): unknown | null {
  if (raw === null) return null;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
}

function mobileSyncValue(value: unknown): unknown | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as { version?: unknown; enabled?: unknown; buttons?: unknown };
  if (raw.version !== 2 || typeof raw.enabled !== "boolean" || !Array.isArray(raw.buttons)) {
    return null;
  }
  return { version: 2, enabled: raw.enabled, buttons: raw.buttons };
}

function mobileDeviceState(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const raw = value as { position?: unknown; dock?: unknown };
  return {
    ...(raw.position !== undefined ? { position: raw.position } : {}),
    ...(raw.dock !== undefined ? { dock: raw.dock } : {}),
  };
}

function localNamespaceValue(namespace: UserPreferenceNamespace): unknown | null {
  if (typeof window === "undefined") return null;
  const { storageKey } = CATEGORY_CONFIG[namespace];
  const raw = window.localStorage.getItem(storageKey);
  if (namespace === "context_indicator") return raw === "compact" ? "compact" : null;
  const parsed = safeParse(raw);
  return namespace === "mobile_assistant" ? mobileSyncValue(parsed) : parsed;
}

export function collectLocalUserPreferences(): UserPreferencesEnvelope {
  const settings: UserPreferencesEnvelope["settings"] = {};
  (Object.keys(CATEGORY_CONFIG) as UserPreferenceNamespace[]).forEach((namespace) => {
    const value = localNamespaceValue(namespace);
    if (value !== null) settings[namespace] = value;
  });
  return { version: 1, settings };
}

function applyNamespace(namespace: UserPreferenceNamespace, value: unknown | null): void {
  const { storageKey, eventName } = CATEGORY_CONFIG[namespace];
  let storedValue = value;
  if (namespace === "mobile_assistant") {
    const local = safeParse(window.localStorage.getItem(storageKey));
    const savedDeviceState = safeParse(
      window.localStorage.getItem(MOBILE_ASSISTANT_DEVICE_STORAGE_KEY),
    );
    const deviceState = {
      ...mobileDeviceState(savedDeviceState),
      ...mobileDeviceState(local),
    };
    if (Object.keys(deviceState).length > 0) {
      window.localStorage.setItem(MOBILE_ASSISTANT_DEVICE_STORAGE_KEY, JSON.stringify(deviceState));
    }
    const syncValue = mobileSyncValue(value);
    storedValue = syncValue === null ? null : { ...(syncValue as object), ...deviceState };
  }
  if (storedValue === null) window.localStorage.removeItem(storageKey);
  else if (namespace === "context_indicator" && storedValue === "compact") {
    window.localStorage.setItem(storageKey, "compact");
  } else {
    window.localStorage.setItem(storageKey, JSON.stringify(storedValue));
  }
  window.dispatchEvent(new Event(eventName));
}

function isEnvelope(value: unknown): value is UserPreferencesEnvelope {
  if (!value || typeof value !== "object") return false;
  const raw = value as { version?: unknown; settings?: unknown };
  return raw.version === 1 && !!raw.settings && typeof raw.settings === "object";
}

function serializedNamespaceValue(
  namespace: UserPreferenceNamespace,
  value: unknown | null,
): string {
  return JSON.stringify(namespace === "mobile_assistant" ? mobileSyncValue(value) : value);
}

function latestNamespaceValue(namespace: UserPreferenceNamespace): unknown | null {
  const dirty = readDirtyValue(namespace);
  return dirty.found ? dirty.value : localNamespaceValue(namespace);
}

function hydrateServerEnvelope(envelope: UserPreferencesEnvelope): void {
  const dirty = readDirtyNamespaces();
  const pendingDirty: UserPreferenceNamespace[] = [];
  applyingServerPreferences = true;
  try {
    for (const namespace of Object.keys(CATEGORY_CONFIG) as UserPreferenceNamespace[]) {
      const serverNamespace = envelope.settings[namespace] ?? null;
      if (dirty.has(namespace)) {
        const retained = readDirtyValue(namespace);
        if (retained.found) applyNamespace(namespace, retained.value);
        pendingDirty.push(namespace);
      } else {
        applyNamespace(namespace, serverNamespace);
        lastAcknowledged.set(namespace, serializedNamespaceValue(namespace, serverNamespace));
      }
    }
  } finally {
    applyingServerPreferences = false;
  }
  for (const namespace of pendingDirty) {
    const retained = readDirtyValue(namespace);
    queueUserPreferencePatch(
      namespace,
      retained.found ? retained.value : localNamespaceValue(namespace),
    );
  }
}

export async function refreshUserPreferencesFromServer(): Promise<void> {
  const fetcher = preferenceFetcher;
  const generation = syncGeneration;
  if (!serverSupportsPreferences || fetcher === null || !preferenceConnectionIsCurrent()) return;
  try {
    const response = await fetcher("/v1/me", { cache: "no-store" });
    if (!isCurrentPreferenceGeneration(generation)) return;
    if (!response.ok) return;
    const data = (await response.json()) as { user_id?: unknown; preferences?: unknown };
    const responseOwner = typeof data.user_id === "string" ? data.user_id : null;
    if (
      !isCurrentPreferenceGeneration(generation) ||
      responseOwner !== activeOwnerId ||
      !isEnvelope(data.preferences)
    )
      return;
    hydrateServerEnvelope(data.preferences);
  } catch {
    // The durable dirty map retains offline edits for the next focus/reload.
  }
}

function refreshOnFocus(): void {
  const now = Date.now();
  if (now - lastFocusRefreshAt < 5000) return;
  lastFocusRefreshAt = now;
  void refreshUserPreferencesFromServer();
}

function refreshOnVisibility(): void {
  if (document.visibilityState === "visible") refreshOnFocus();
}

function installRefreshListeners(): void {
  if (refreshListenersInstalled) return;
  window.addEventListener("focus", refreshOnFocus);
  document.addEventListener("visibilitychange", refreshOnVisibility);
  refreshListenersInstalled = true;
}

function cancelPreferenceActivity(): void {
  for (const timer of pendingTimers.values()) window.clearTimeout(timer);
  pendingTimers.clear();
  patchesAfterFlight.clear();
  inFlightPatches.clear();
  lastAcknowledged.clear();
  syncGeneration += 1;
}

function downgradeToDeviceOnlyScope(): void {
  cancelPreferenceActivity();
  serverSupportsPreferences = false;
  const preferenceScope = dirtyOwnerKey();
  const previousScope = window.localStorage.getItem(USER_PREFERENCES_SCOPE_KEY);
  if (previousScope !== null && previousScope !== preferenceScope) {
    hydrateServerEnvelope({ version: 1, settings: {} });
  }
  window.localStorage.setItem(USER_PREFERENCES_SCOPE_KEY, preferenceScope);
}

export function deactivateUserPreferencesSync(): void {
  cancelPreferenceActivity();
  serverSupportsPreferences = false;
  preferenceFetcher = null;
}

export async function initializeUserPreferencesSync(
  serverValue: unknown | null | undefined,
  fetcher: PreferenceFetcher,
  ownerId: string | null = null,
  serverId = "__default__",
  connectionId = serverId,
  isCurrentConnection: () => boolean = () => true,
): Promise<void> {
  if (
    activeOwnerId !== ownerId ||
    activeServerId !== serverId ||
    activeConnectionId !== connectionId
  ) {
    cancelPreferenceActivity();
  }
  activeOwnerId = ownerId;
  activeServerId = serverId;
  activeConnectionId = connectionId;
  preferenceFetcher = fetcher;
  preferenceConnectionIsCurrent = isCurrentConnection;
  if (serverValue === undefined) {
    // A native client can switch from a newer Server to an older one without
    // reloading the SPA. Cancel writes queued for the previous Server and
    // return to device-only behavior until capability is observed again.
    downgradeToDeviceOnlyScope();
    return;
  }
  if (serverValue !== null && !isEnvelope(serverValue)) {
    downgradeToDeviceOnlyScope();
    return;
  }
  serverSupportsPreferences = true;
  installRefreshListeners();
  if (serverValue === null) {
    const generation = syncGeneration;
    try {
      const preferenceScope = dirtyOwnerKey();
      const previousScope = window.localStorage.getItem(USER_PREFERENCES_SCOPE_KEY);
      // A browser profile can use more than one account or embedded Server.
      // Never seed a new scope from another scope's origin-wide localStorage,
      // and clear it before the network call so a failed PUT cannot expose the
      // previous user's values to the newly resolved identity.
      const scopeChanged = previousScope !== null && previousScope !== preferenceScope;
      if (scopeChanged) {
        hydrateServerEnvelope({ version: 1, settings: {} });
        window.localStorage.setItem(USER_PREFERENCES_SCOPE_KEY, preferenceScope);
      }
      const initialEnvelope = scopeChanged
        ? { version: 1 as const, settings: {} }
        : collectLocalUserPreferences();
      const response = await fetcher("/v1/me/preferences", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(initialEnvelope),
      });
      if (!isCurrentPreferenceGeneration(generation)) return;
      if (response.ok) {
        window.localStorage.setItem(USER_PREFERENCES_SCOPE_KEY, preferenceScope);
        const persisted = (await response.json().catch(() => null)) as unknown;
        if (!isCurrentPreferenceGeneration(generation)) return;
        // A concurrent first device may have initialized the account first.
        // Hydrate the winning server envelope immediately instead of waiting
        // for a reload while showing stale device settings.
        if (isEnvelope(persisted)) {
          hydrateServerEnvelope(persisted);
        }
      }
    } catch {
      // Offline cache remains authoritative until the next successful boot.
    }
    return;
  }
  if (!isEnvelope(serverValue)) return;
  window.localStorage.setItem(USER_PREFERENCES_SCOPE_KEY, dirtyOwnerKey());
  hydrateServerEnvelope(serverValue);
}

function schedulePatch(
  namespace: UserPreferenceNamespace,
  syncValue: unknown | null,
  serialized: string,
  delayMs: number,
  attempt: number,
): void {
  const generation = syncGeneration;
  const fetcher = preferenceFetcher;
  const existing = pendingTimers.get(namespace);
  if (existing !== undefined) window.clearTimeout(existing);
  pendingTimers.set(
    namespace,
    window.setTimeout(() => {
      pendingTimers.delete(namespace);
      if (!isCurrentPreferenceGeneration(generation)) return;
      if (inFlightPatches.has(namespace)) {
        patchesAfterFlight.set(namespace, { syncValue, serialized, delayMs: 0, attempt });
        return;
      }
      if (lastAcknowledged.get(namespace) === serialized && !readDirtyNamespaces().has(namespace)) {
        return;
      }
      if (fetcher === null) return;
      inFlightPatches.set(namespace, generation);
      void (async () => {
        try {
          const response = await fetcher(`/v1/me/preferences/${namespace}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: syncValue }),
          });
          if (!isCurrentPreferenceGeneration(generation)) return;
          if (!response?.ok) throw new Error("preference patch failed");
          lastAcknowledged.set(namespace, serialized);
          const current = latestNamespaceValue(namespace);
          const currentSerialized = serializedNamespaceValue(namespace, current);
          if (currentSerialized === serialized) {
            clearDirty(namespace);
          } else {
            markDirty(namespace, current);
            patchesAfterFlight.set(namespace, {
              syncValue: current,
              serialized: currentSerialized,
              delayMs: 0,
              attempt: 0,
            });
          }
        } catch {
          if (isCurrentPreferenceGeneration(generation)) {
            const current = latestNamespaceValue(namespace);
            const currentSerialized = serializedNamespaceValue(namespace, current);
            if (currentSerialized !== serialized) {
              markDirty(namespace, current);
              patchesAfterFlight.set(namespace, {
                syncValue: current,
                serialized: currentSerialized,
                delayMs: 0,
                attempt: 0,
              });
            } else if (attempt < 3 && !patchesAfterFlight.has(namespace)) {
              patchesAfterFlight.set(namespace, {
                syncValue,
                serialized,
                delayMs: 1000 * 2 ** attempt,
                attempt: attempt + 1,
              });
            }
          }
        } finally {
          if (inFlightPatches.get(namespace) === generation) inFlightPatches.delete(namespace);
          if (isCurrentPreferenceGeneration(generation)) {
            const next = patchesAfterFlight.get(namespace);
            if (next) {
              patchesAfterFlight.delete(namespace);
              schedulePatch(namespace, next.syncValue, next.serialized, next.delayMs, next.attempt);
            }
          }
        }
      })();
    }, delayMs),
  );
}

export function queueUserPreferencePatch(
  namespace: UserPreferenceNamespace,
  value: unknown | null,
): void {
  if (
    typeof window === "undefined" ||
    applyingServerPreferences ||
    !serverSupportsPreferences ||
    !preferenceConnectionIsCurrent() ||
    preferenceFetcher === null
  ) {
    return;
  }
  const syncValue = namespace === "mobile_assistant" ? mobileSyncValue(value) : value;
  const serialized = JSON.stringify(syncValue);
  if (lastAcknowledged.get(namespace) === serialized && !readDirtyNamespaces().has(namespace))
    return;
  markDirty(namespace, syncValue);
  schedulePatch(namespace, syncValue, serialized, 250, 0);
}

export function syncAllUserPreferencesFromLocal(): void {
  const envelope = collectLocalUserPreferences();
  for (const namespace of Object.keys(CATEGORY_CONFIG) as UserPreferenceNamespace[]) {
    queueUserPreferencePatch(namespace, envelope.settings[namespace] ?? null);
    window.dispatchEvent(new Event(CATEGORY_CONFIG[namespace].eventName));
  }
}

/** Test-only reset for module-scoped boot/debounce state. */
export function resetUserPreferencesSyncForTests(): void {
  for (const timer of pendingTimers.values()) window.clearTimeout(timer);
  pendingTimers.clear();
  patchesAfterFlight.clear();
  inFlightPatches.clear();
  lastAcknowledged.clear();
  preferenceFetcher = null;
  serverSupportsPreferences = false;
  applyingServerPreferences = false;
  activeOwnerId = null;
  activeServerId = "__default__";
  activeConnectionId = "__default__";
  syncGeneration = 0;
  preferenceConnectionIsCurrent = () => true;
  lastFocusRefreshAt = 0;
  if (refreshListenersInstalled) {
    window.removeEventListener("focus", refreshOnFocus);
    document.removeEventListener("visibilitychange", refreshOnVisibility);
    refreshListenersInstalled = false;
  }
}
