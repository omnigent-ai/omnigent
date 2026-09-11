import { toast } from "sonner";
import { randomUUID } from "./randomUUID";
import { landingStorageKey } from "./landingStorage";
import { useSyncExternalStore } from "react";
import type { SessionWorkspaceState } from "./sessionWorkspaceState";
import { writeSessionWorkspaceState } from "./sessionWorkspaceState";
import type { WorkspaceResourceTarget } from "./workspaceTarget";

const STORAGE_KEY = "omnigent:landing-workspace";
export interface LandingWorkspaceSelection {
  hostId: string | null;
  workspace: string;
  available: boolean;
  reason: string;
}
interface LandingWorkspaceState {
  browserNamespace: string;
  selection: LandingWorkspaceSelection | null;
  panel: SessionWorkspaceState;
  starting?: boolean;
  busy?: boolean;
}
function initialState(): LandingWorkspaceState {
  try {
    const saved = JSON.parse(localStorage.getItem(landingStorageKey(STORAGE_KEY)) ?? "null");
    if (saved?.browserNamespace?.startsWith("draft-workspace:") && saved.panel)
      return { ...saved, starting: false, busy: false };
  } catch {
    /* Storage is optional. */
  }
  return { browserNamespace: `draft-workspace:${randomUUID()}`, selection: null, panel: {} };
}
let scope = landingStorageKey(STORAGE_KEY);
let state = initialState();
const listeners = new Set<() => void>();
function publish(next: LandingWorkspaceState) {
  state = next;
  try {
    localStorage.setItem(scope, JSON.stringify(next));
  } catch {
    /* Storage is optional. */
  }
  listeners.forEach((listener) => listener());
}
export function readLandingWorkspaceState() {
  const currentScope = landingStorageKey(STORAGE_KEY);
  if (currentScope !== scope) {
    scope = currentScope;
    state = initialState();
    resourceLifecycle = null;
    resourceLifecycleNamespace = null;
  }
  return state;
}
export function useLandingWorkspaceState() {
  return useSyncExternalStore((listener) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }, readLandingWorkspaceState);
}
const pendingStarts = new Map<string, { scope: string; namespace: string; contextId?: string }>();
const claimedNamespaces = new Map<string, string>();
export function setLandingWorkspaceStarting(starting: boolean) {
  readLandingWorkspaceState();
  if (!starting) {
    for (const [token, pending] of pendingStarts) {
      if (pending.scope !== landingStorageKey(STORAGE_KEY)) continue;
      pendingStarts.delete(token);
      if (claimedNamespaces.get(pending.namespace) === token)
        claimedNamespaces.delete(pending.namespace);
    }
  }
  if (Boolean(state.starting) !== starting) publish({ ...state, starting });
}
export function setLandingWorkspaceBusy(busy: boolean) {
  readLandingWorkspaceState();
  if (Boolean(state.busy) !== busy) publish({ ...state, busy });
}
export function writeLandingWorkspacePanel(patch: SessionWorkspaceState) {
  readLandingWorkspaceState();
  publish({ ...state, panel: { ...state.panel, ...patch } });
}
async function closeLandingBrowsers(draft: LandingWorkspaceState) {
  const bridge = (
    window as unknown as {
      omnigentDesktop?: { browserClose?: (id: string) => Promise<{ ok: boolean }> };
    }
  ).omnigentDesktop;
  if (!bridge?.browserClose) return;
  const ids = [
    draft.browserNamespace,
    ...(draft.panel.openBrowsers ?? []).map(
      (id) => `browser-tab:${encodeURIComponent(draft.browserNamespace)}:${id}`,
    ),
  ];
  const results = await Promise.all(ids.map((id) => bridge.browserClose!(id)));
  if (results.some((result) => !result.ok)) throw new Error("Draft browser cleanup failed");
}
export async function discardLandingWorkspace(snapshot = captureLandingWorkspace()) {
  const draft =
    readLandingWorkspaceState().browserNamespace === snapshot.state.browserNamespace
      ? state
      : snapshot.state;
  await snapshot.lifecycle?.discard();
  await closeLandingBrowsers(draft);
  if (readLandingWorkspaceState().browserNamespace === draft.browserNamespace)
    publish({
      browserNamespace: `draft-workspace:${randomUUID()}`,
      selection: null,
      panel: {},
      starting: state.starting,
    });
}
export async function discardAbandonedLandingWorkspace(
  snapshot: ReturnType<typeof captureLandingWorkspace>,
  preserveTerminals = false,
) {
  if (readLandingWorkspaceState().browserNamespace === snapshot.state.browserNamespace) return;
  const results = await Promise.allSettled([
    preserveTerminals ? Promise.resolve() : snapshot.lifecycle?.discard(),
    closeLandingBrowsers(snapshot.state),
  ]);
  const failure = results.find((result) => result.status === "rejected");
  if (failure?.status === "rejected") throw failure.reason;
}
export function publishLandingWorkspaceSelection(selection: LandingWorkspaceSelection) {
  readLandingWorkspaceState();
  if (JSON.stringify(state.selection) === JSON.stringify(selection)) return;
  const changed =
    state.selection?.hostId !== selection.hostId ||
    state.selection?.workspace !== selection.workspace;
  const claimed = claimedNamespaces.has(state.browserNamespace);
  if (
    changed &&
    !claimed &&
    resourceLifecycleNamespace === state.browserNamespace &&
    resourceLifecycle &&
    !discardingNamespaces.has(state.browserNamespace)
  ) {
    void resourceLifecycle
      .discard()
      .catch(() => toast.error("Couldn't close the previous workspace's draft context."));
  }
  if (changed && !claimed)
    void closeLandingBrowsers(state).catch(() =>
      toast.error("Couldn't close the previous workspace's browser tabs."),
    );
  publish({
    ...state,
    browserNamespace: changed ? `draft-workspace:${randomUUID()}` : state.browserNamespace,
    selection,
    panel: changed
      ? {
          ...state.panel,
          openFiles: [],
          selectedFilePath: null,
          selectedTerminalKey: null,
          openBrowsers: [],
          selectedBrowserId: null,
        }
      : state.panel,
  });
}
export function landingResourceTarget(
  selection: LandingWorkspaceSelection | null,
): WorkspaceResourceTarget | undefined {
  return selection?.available && selection.hostId
    ? { kind: "host", hostId: selection.hostId, workspace: selection.workspace }
    : undefined;
}
let resourceLifecycleNamespace: string | null = null;
let resourceLifecycle: {
  contextId?: string;
  hasTerminals: () => boolean;
  discard: () => Promise<void>;
  discardSnapshot?: () => Promise<void>;
  adopt: (sessionId: string) => Promise<unknown>;
} | null = null;
export function registerLandingResourceLifecycle(lifecycle: NonNullable<typeof resourceLifecycle>) {
  readLandingWorkspaceState();
  resourceLifecycle = lifecycle;
  resourceLifecycleNamespace = state.browserNamespace;
  return () => {
    if (resourceLifecycle === lifecycle) resourceLifecycle = null;
  };
}
export function captureLandingWorkspace() {
  readLandingWorkspaceState();
  return {
    state,
    lifecycle:
      resourceLifecycle && resourceLifecycleNamespace === state.browserNamespace
        ? {
            ...resourceLifecycle,
            discard: resourceLifecycle.discardSnapshot ?? resourceLifecycle.discard,
          }
        : null,
  };
}
export function claimLandingWorkspaceStart() {
  const snapshot = captureLandingWorkspace();
  const token = randomUUID();
  const namespace = snapshot.state.browserNamespace;
  const ownsResources = !claimedNamespaces.has(namespace);
  if (ownsResources) claimedNamespaces.set(namespace, token);
  pendingStarts.set(token, {
    scope: landingStorageKey(STORAGE_KEY),
    namespace,
    contextId: ownsResources ? snapshot.lifecycle?.contextId : undefined,
  });
  setLandingWorkspaceStarting(true);
  return { snapshot, token, ownsResources };
}
export function landingContextClaimedElsewhere(contextId: string | undefined, namespace: string) {
  return (
    !!contextId &&
    [...pendingStarts.values()].some(
      (pending) =>
        pending.scope === landingStorageKey(STORAGE_KEY) &&
        pending.contextId === contextId &&
        pending.namespace !== namespace,
    )
  );
}
export function finishLandingWorkspaceStart(token: string) {
  readLandingWorkspaceState();
  const request = pendingStarts.get(token);
  if (!request) return;
  pendingStarts.delete(token);
  if (claimedNamespaces.get(request.namespace) === token)
    claimedNamespaces.delete(request.namespace);
  if (request.scope !== landingStorageKey(STORAGE_KEY)) return;
  const starting = [...pendingStarts.values()].some((pending) => pending.scope === request.scope);
  if (Boolean(state.starting) !== starting) publish({ ...state, starting });
}
const discardingNamespaces = new Set<string>();
export function confirmLandingWorkspaceChange(): boolean {
  readLandingWorkspaceState();
  if (
    discardingNamespaces.has(state.browserNamespace) ||
    claimedNamespaces.has(state.browserNamespace)
  )
    return true;
  if (resourceLifecycleNamespace !== state.browserNamespace || !resourceLifecycle?.hasTerminals())
    return true;
  if (
    !window.confirm(
      "Changing workspace will close the running draft terminals. Close them and change workspace?",
    )
  )
    return false;
  const namespace = state.browserNamespace;
  discardingNamespaces.add(namespace);
  void resourceLifecycle
    .discard()
    .catch(() =>
      toast.error("Couldn't close draft shells. Return to the previous workspace to retry."),
    )
    .finally(() => {
      discardingNamespaces.delete(namespace);
    });
  return true;
}
export class LandingWorkspaceAdoptionError extends Error {
  readonly terminalsTransferred: boolean;
  constructor(terminalsTransferred: boolean, cause: unknown) {
    super(cause instanceof Error ? cause.message : "Draft tool transfer failed", { cause });
    this.terminalsTransferred = terminalsTransferred;
  }
}
export async function adoptLandingWorkspace(
  sessionId: string,
  hostId: string | null,
  workspace: string,
  snapshot = captureLandingWorkspace(),
) {
  const draft =
    readLandingWorkspaceState().browserNamespace === snapshot.state.browserNamespace
      ? state
      : snapshot.state;
  if (
    !draft.selection?.available ||
    draft.selection.hostId !== hostId ||
    draft.selection.workspace !== workspace
  )
    return;
  const transferredTerminals = snapshot.lifecycle?.hasTerminals() ?? false;
  try {
    await snapshot.lifecycle?.adopt(sessionId);
  } catch (error) {
    throw new LandingWorkspaceAdoptionError(false, error);
  }
  if (transferredTerminals)
    writeSessionWorkspaceState(sessionId, {
      ...draft.panel,
      openBrowsers: [],
      selectedBrowserId: null,
    });
  const bridge = (
    window as unknown as {
      omnigentDesktop?: {
        browserAdoptDraft?: (source: string, target: string) => Promise<{ ok: boolean }>;
      };
    }
  ).omnigentDesktop;
  try {
    const browserResult = await bridge?.browserAdoptDraft?.(draft.browserNamespace, sessionId);
    if (browserResult?.ok === false) throw new Error("Draft browser transfer failed");
  } catch (error) {
    throw new LandingWorkspaceAdoptionError(transferredTerminals, error);
  }
  writeSessionWorkspaceState(sessionId, draft.panel);
  if (readLandingWorkspaceState().browserNamespace !== draft.browserNamespace) return;
  publish({
    browserNamespace: `draft-workspace:${randomUUID()}`,
    selection: null,
    panel: {},
    starting: state.starting,
  });
}
