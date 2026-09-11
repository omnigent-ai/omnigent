import { useCallback, useEffect, useRef, useState } from "react";

import { authenticatedFetch } from "@/lib/identity";
import { landingStorageKey } from "@/lib/landingStorage";
import { terminalInfoFromResource, type TerminalInfo } from "@/lib/terminals";
import { randomUUID } from "@/lib/randomUUID";

const STORAGE_PREFIX = "omnigent:draft-workspace-contexts:v1";

/** Draft contexts are kept alive while the app is open, including after handoff. */
export const DRAFT_WORKSPACE_HEARTBEAT_INTERVAL_MS = 60_000;
export const DRAFT_TERMINALS_POLL_INTERVAL_MS = 2_500;

interface WorkspaceContextWire {
  id: string;
  workspace: string;
  session_id: string | null;
  lease_seconds: number;
}

export interface DraftWorkspaceContext extends WorkspaceContextWire {
  /** Host identity paired with the canonical workspace path in browser storage. */
  hostId: string;
  /** User-selected paths that resolve to the canonical server workspace. */
  workspaceAliases: string[];
}

export type DraftTerminalInfo = TerminalInfo;

interface PersistedDraftContext {
  id: string;
  workspace: string;
  hostId: string;
  leaseSeconds: number;
  sessionId: string | null;
  workspaceAliases?: string[];
}

export interface UseDraftWorkspaceResult {
  context: DraftWorkspaceContext | null;
  terminals: DraftTerminalInfo[];
  isLoading: boolean;
  error: Error | null;
  ensureContext: (hostId: string, workspace: string) => Promise<DraftWorkspaceContext>;
  refreshTerminals: (context?: DraftWorkspaceContext) => Promise<DraftTerminalInfo[]>;
  createTerminal: (context?: DraftWorkspaceContext) => Promise<DraftTerminalInfo>;
  deleteTerminal: (terminalId: string, context?: DraftWorkspaceContext) => Promise<void>;
  discard: (context?: DraftWorkspaceContext | null) => Promise<void>;
  adopt: (sessionId: string, context?: DraftWorkspaceContext) => Promise<DraftWorkspaceContext>;
}

function contextBasePath(hostId: string, contextId: string): string {
  return (
    `/v1/hosts/${encodeURIComponent(hostId)}/workspace-contexts/` + encodeURIComponent(contextId)
  );
}

class DraftWorkspaceHttpError extends Error {
  readonly status: number;

  constructor(prefix: string, status: number, statusText: string) {
    super(`${prefix}: ${status} ${statusText}`);
    this.status = status;
  }
}

function httpError(prefix: string, response: Response): DraftWorkspaceHttpError {
  return new DraftWorkspaceHttpError(prefix, response.status, response.statusText);
}

function isWorkspaceContextWire(value: unknown): value is WorkspaceContextWire {
  if (value === null || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  return (
    typeof row.id === "string" &&
    row.id.length > 0 &&
    typeof row.workspace === "string" &&
    (row.session_id === null || typeof row.session_id === "string") &&
    typeof row.lease_seconds === "number"
  );
}

function withHost(
  context: WorkspaceContextWire,
  hostId: string,
  aliases: readonly string[] = [],
): DraftWorkspaceContext {
  return {
    ...context,
    hostId,
    workspaceAliases: [...new Set([context.workspace, ...aliases])],
  };
}

function readPersistedContexts(storageKey: string): PersistedDraftContext[] {
  if (typeof window === "undefined") return [];
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(storageKey) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((value): value is PersistedDraftContext => {
      if (value === null || typeof value !== "object") return false;
      const row = value as Record<string, unknown>;
      return (
        typeof row.id === "string" &&
        row.id.length > 0 &&
        typeof row.workspace === "string" &&
        typeof row.hostId === "string" &&
        row.hostId.length > 0 &&
        typeof row.leaseSeconds === "number" &&
        (row.sessionId === null || typeof row.sessionId === "string") &&
        (row.workspaceAliases === undefined ||
          (Array.isArray(row.workspaceAliases) &&
            row.workspaceAliases.every((alias) => typeof alias === "string")))
      );
    });
  } catch {
    return [];
  }
}

function writePersistedContexts(storageKey: string, contexts: PersistedDraftContext[]): void {
  if (typeof window === "undefined") return;
  try {
    if (contexts.length === 0) {
      window.localStorage.removeItem(storageKey);
    } else {
      window.localStorage.setItem(storageKey, JSON.stringify(contexts));
    }
  } catch {
    // Storage is best-effort; the in-memory context remains usable.
  }
}

function persistContext(storageKey: string, context: DraftWorkspaceContext): void {
  const existing = readPersistedContexts(storageKey).filter(
    (row) =>
      row.id !== context.id &&
      !(
        context.session_id === null &&
        row.sessionId === null &&
        row.hostId === context.hostId &&
        row.workspace === context.workspace
      ) &&
      !(context.session_id !== null && row.sessionId === context.session_id),
  );
  existing.push({
    id: context.id,
    workspace: context.workspace,
    hostId: context.hostId,
    leaseSeconds: context.lease_seconds,
    sessionId: context.session_id,
    workspaceAliases: context.workspaceAliases,
  });
  writePersistedContexts(storageKey, existing);
}

function forgetContext(storageKey: string, contextId: string): void {
  writePersistedContexts(
    storageKey,
    readPersistedContexts(storageKey).filter((row) => row.id !== contextId),
  );
}

function restoredContext(row: PersistedDraftContext): DraftWorkspaceContext {
  return {
    id: row.id,
    workspace: row.workspace,
    hostId: row.hostId,
    session_id: row.sessionId,
    lease_seconds: row.leaseSeconds,
    workspaceAliases: [...new Set([row.workspace, ...(row.workspaceAliases ?? [])])],
  };
}

function contextMatchesTarget(
  context: Pick<DraftWorkspaceContext, "hostId" | "workspace" | "workspaceAliases">,
  hostId: string,
  workspace: string,
): boolean {
  return (
    context.hostId === hostId &&
    (context.workspace === workspace || context.workspaceAliases.includes(workspace))
  );
}

function persistedContextMatchesTarget(
  context: PersistedDraftContext,
  hostId: string,
  workspace: string,
): boolean {
  return (
    context.hostId === hostId &&
    (context.workspace === workspace || context.workspaceAliases?.includes(workspace) === true)
  );
}

export async function createDraftWorkspaceContext(
  hostId: string,
  workspace: string,
): Promise<DraftWorkspaceContext> {
  const response = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/workspace-contexts`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace }),
    },
  );
  if (!response.ok) throw httpError("draft workspace create failed", response);
  const body: unknown = await response.json();
  if (!isWorkspaceContextWire(body)) {
    throw new Error("draft workspace create returned an unrecognized context shape");
  }
  return withHost(body, hostId, [workspace]);
}

export async function heartbeatDraftWorkspaceContext(
  context: DraftWorkspaceContext,
): Promise<DraftWorkspaceContext> {
  const response = await authenticatedFetch(
    `${contextBasePath(context.hostId, context.id)}/heartbeat`,
    {
      method: "POST",
    },
  );
  if (!response.ok) throw httpError("draft workspace heartbeat failed", response);
  const body: unknown = await response.json();
  if (!isWorkspaceContextWire(body)) {
    throw new Error("draft workspace heartbeat returned an unrecognized context shape");
  }
  return withHost(body, context.hostId, context.workspaceAliases);
}

export async function fetchDraftTerminals(
  hostId: string,
  contextId: string,
): Promise<DraftTerminalInfo[]> {
  const response = await authenticatedFetch(
    `${contextBasePath(hostId, contextId)}/resources/terminals`,
  );
  if (!response.ok) throw httpError("draft terminals fetch failed", response);
  const body = (await response.json()) as { data?: unknown };
  const rows = Array.isArray(body.data) ? body.data : [];
  const terminals: DraftTerminalInfo[] = [];
  for (const row of rows) {
    if (row !== null && typeof row === "object") {
      const terminal = terminalInfoFromResource(row as Record<string, unknown>);
      if (terminal !== null) terminals.push(terminal);
    }
  }
  return terminals;
}

export async function createDraftTerminal(
  hostId: string,
  contextId: string,
): Promise<DraftTerminalInfo> {
  const response = await authenticatedFetch(
    `${contextBasePath(hostId, contextId)}/resources/terminals`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ terminal: "bash", session_key: `draft-${randomUUID()}` }),
    },
  );
  if (!response.ok) throw httpError("draft terminal create failed", response);
  const body = (await response.json()) as Record<string, unknown>;
  const terminal = terminalInfoFromResource(body);
  if (terminal === null) {
    throw new Error("draft terminal create returned an unrecognized resource shape");
  }
  return terminal;
}

export async function deleteDraftTerminal(
  hostId: string,
  contextId: string,
  terminalId: string,
): Promise<void> {
  const response = await authenticatedFetch(
    `${contextBasePath(hostId, contextId)}/resources/terminals/${encodeURIComponent(terminalId)}`,
    { method: "DELETE" },
  );
  if (!response.ok && response.status !== 404) {
    throw httpError("draft terminal delete failed", response);
  }
}

export async function discardDraftWorkspaceContext(
  hostId: string,
  contextId: string,
): Promise<void> {
  const response = await authenticatedFetch(contextBasePath(hostId, contextId), {
    method: "DELETE",
  });
  if (!response.ok && response.status !== 404) {
    throw httpError("draft workspace discard failed", response);
  }
}

export async function adoptDraftWorkspaceContext(
  context: DraftWorkspaceContext,
  sessionId: string,
): Promise<DraftWorkspaceContext> {
  const response = await authenticatedFetch(
    `${contextBasePath(context.hostId, context.id)}/handoff`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    },
  );
  if (!response.ok) throw httpError("draft workspace handoff failed", response);
  const body: unknown = await response.json();
  if (!isWorkspaceContextWire(body)) {
    throw new Error("draft workspace handoff returned an unrecognized context shape");
  }
  return withHost(body, context.hostId, context.workspaceAliases);
}

/** Build the explicit WebSocket route consumed by TerminalView's attachPath prop. */
export function buildDraftTerminalAttachPath(
  contextId: string,
  terminalId: string,
  readOnly: boolean,
  hostId: string,
): string {
  const path =
    `${contextBasePath(hostId, contextId)}/resources/terminals/` +
    `${encodeURIComponent(terminalId)}/attach`;
  const params = new URLSearchParams();
  if (readOnly) params.set("read_only", "true");
  params.set("omnigent_slice_key", hostId);
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

function noActiveContextError(): Error {
  return new Error("No draft workspace context is active");
}

export function useDraftWorkspace(sessionId?: string | null): UseDraftWorkspaceResult {
  const currentStorageKey = landingStorageKey(STORAGE_PREFIX);
  const [storageKey, setStorageKey] = useState(currentStorageKey);
  const [contexts, setContexts] = useState<DraftWorkspaceContext[]>(() =>
    readPersistedContexts(storageKey).map(restoredContext),
  );
  const [terminalsByContext, setTerminalsByContext] = useState<Record<string, DraftTerminalInfo[]>>(
    {},
  );
  const [isLoading, setIsLoading] = useState(false);
  const [pendingTerminalCreates, setPendingTerminalCreates] = useState<Set<string>>(
    () => new Set(),
  );
  const [error, setError] = useState<Error | null>(null);
  const contextsRef = useRef(contexts);
  contextsRef.current = contexts;
  const activeStorageKeyRef = useRef(currentStorageKey);
  activeStorageKeyRef.current = currentStorageKey;
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const scopeMatches = storageKey === currentStorageKey;
  const context =
    (scopeMatches
      ? contexts.find((row) =>
          sessionId == null ? row.session_id === null : row.session_id === sessionId,
        )
      : undefined) ?? null;
  const contextRef = useRef<DraftWorkspaceContext | null>(context);
  contextRef.current = context;
  const ensureFlightsRef = useRef(new Map<string, Promise<DraftWorkspaceContext>>());
  const transitionTailRef = useRef<Promise<void>>(Promise.resolve());
  const transitionGenerationRef = useRef(0);
  const tombstonesRef = useRef(new Set<string>());
  const discardFlightsRef = useRef(new Map<string, Promise<void>>());
  const terminalMutationGenerationRef = useRef(new Map<string, number>());
  const terminalRequestSequenceRef = useRef(new Map<string, number>());
  const terminalAppliedRequestRef = useRef(new Map<string, number>());
  const terminalInventoryLoaded = context === null || Object.hasOwn(terminalsByContext, context.id);

  useEffect(() => {
    if (scopeMatches) return;
    const restored = readPersistedContexts(currentStorageKey).map(restoredContext);
    transitionGenerationRef.current += 1;
    ensureFlightsRef.current.clear();
    discardFlightsRef.current.clear();
    terminalMutationGenerationRef.current.clear();
    terminalRequestSequenceRef.current.clear();
    terminalAppliedRequestRef.current.clear();
    tombstonesRef.current.clear();
    transitionTailRef.current = Promise.resolve();
    contextsRef.current = restored;
    setContexts(restored);
    setTerminalsByContext({});
    setIsLoading(false);
    setPendingTerminalCreates(new Set());
    setError(null);
    setStorageKey(currentStorageKey);
  }, [currentStorageKey, scopeMatches]);

  const upsertContext = useCallback(
    (next: DraftWorkspaceContext) => {
      if (activeStorageKeyRef.current !== storageKey) return false;
      if (tombstonesRef.current.has(next.id)) return false;
      const updated = [...contextsRef.current.filter((row) => row.id !== next.id), next];
      contextsRef.current = updated;
      contextRef.current =
        updated.find((row) =>
          sessionIdRef.current == null
            ? row.session_id === null
            : row.session_id === sessionIdRef.current,
        ) ?? null;
      setContexts(updated);
      persistContext(storageKey, next);
      setError(null);
      return true;
    },
    [storageKey],
  );

  const clearContext = useCallback(
    (contextId: string) => {
      if (activeStorageKeyRef.current !== storageKey) return;
      tombstonesRef.current.add(contextId);
      forgetContext(storageKey, contextId);
      const updated = contextsRef.current.filter((row) => row.id !== contextId);
      contextsRef.current = updated;
      contextRef.current =
        updated.find((row) =>
          sessionIdRef.current == null
            ? row.session_id === null
            : row.session_id === sessionIdRef.current,
        ) ?? null;
      setContexts(updated);
      setTerminalsByContext((current) => {
        const { [contextId]: _removed, ...remaining } = current;
        return remaining;
      });
    },
    [storageKey],
  );

  const discardContext = useCallback(
    (target: DraftWorkspaceContext): Promise<void> => {
      const existing = discardFlightsRef.current.get(target.id);
      if (existing) return existing;
      clearContext(target.id);
      const flight = discardDraftWorkspaceContext(target.hostId, target.id);
      discardFlightsRef.current.set(target.id, flight);
      void flight.then(
        () => discardFlightsRef.current.delete(target.id),
        () => discardFlightsRef.current.delete(target.id),
      );
      return flight;
    },
    [clearContext],
  );

  const ensureContext = useCallback(
    (hostId: string, workspace: string): Promise<DraftWorkspaceContext> => {
      if (!hostId || !workspace) {
        return Promise.reject(new Error("A host and workspace are required for a draft terminal"));
      }
      const key = JSON.stringify([hostId, workspace]);
      const existingFlight = ensureFlightsRef.current.get(key);
      if (existingFlight) return existingFlight;

      const generation = (transitionGenerationRef.current += 1);
      setIsLoading(true);
      const run = async (): Promise<DraftWorkspaceContext> => {
        let cleanupError: Error | null = null;
        if (generation !== transitionGenerationRef.current) {
          throw new Error("Draft workspace selection changed");
        }
        const active = contextsRef.current.find((row) => row.session_id === null) ?? null;
        if (active && contextMatchesTarget(active, hostId, workspace)) return active;

        const persisted = readPersistedContexts(storageKey).find(
          (row) => row.sessionId === null && persistedContextMatchesTarget(row, hostId, workspace),
        );
        if (persisted) {
          try {
            const restored = await heartbeatDraftWorkspaceContext(restoredContext(persisted));
            if (generation !== transitionGenerationRef.current) {
              throw new Error("Draft workspace selection changed");
            }
            if (!upsertContext(restored)) throw new Error("Draft workspace selection changed");
            return restored;
          } catch (restoreError) {
            if (restoreError instanceof DraftWorkspaceHttpError && restoreError.status === 404) {
              forgetContext(storageKey, persisted.id);
            } else {
              throw restoreError;
            }
          }
        }
        if (active !== null) {
          await discardContext(active).catch((cause: unknown) => {
            cleanupError = cause instanceof Error ? cause : new Error(String(cause));
          });
          if (generation !== transitionGenerationRef.current) {
            throw new Error("Draft workspace selection changed");
          }
        }
        const created = await createDraftWorkspaceContext(hostId, workspace);
        if (generation !== transitionGenerationRef.current) {
          tombstonesRef.current.add(created.id);
          await discardDraftWorkspaceContext(created.hostId, created.id);
          throw new Error("Draft workspace selection changed");
        }
        tombstonesRef.current.delete(created.id);
        if (!upsertContext(created)) {
          tombstonesRef.current.add(created.id);
          await discardDraftWorkspaceContext(created.hostId, created.id);
          throw new Error("Draft workspace selection changed");
        }
        if (cleanupError !== null) setError(cleanupError);
        return created;
      };
      const flight = transitionTailRef.current.then(run);
      transitionTailRef.current = flight.then(
        () => undefined,
        () => undefined,
      );
      ensureFlightsRef.current.set(key, flight);
      void flight
        .catch((cause: unknown) => {
          setError(cause instanceof Error ? cause : new Error(String(cause)));
        })
        .finally(() => {
          ensureFlightsRef.current.delete(key);
          setIsLoading(ensureFlightsRef.current.size > 0);
        });
      return flight;
    },
    [discardContext, storageKey, upsertContext],
  );

  const refreshTerminals = useCallback(
    async (targetContext?: DraftWorkspaceContext): Promise<DraftTerminalInfo[]> => {
      const active = targetContext ?? contextRef.current;
      if (active === null) return [];
      const operationStorageKey = activeStorageKeyRef.current;
      const mutationGeneration = terminalMutationGenerationRef.current.get(active.id) ?? 0;
      const requestSequence = (terminalRequestSequenceRef.current.get(active.id) ?? 0) + 1;
      terminalRequestSequenceRef.current.set(active.id, requestSequence);
      try {
        const next = await fetchDraftTerminals(active.hostId, active.id);
        if (
          activeStorageKeyRef.current === operationStorageKey &&
          !tombstonesRef.current.has(active.id) &&
          (terminalMutationGenerationRef.current.get(active.id) ?? 0) === mutationGeneration &&
          requestSequence > (terminalAppliedRequestRef.current.get(active.id) ?? 0)
        ) {
          terminalAppliedRequestRef.current.set(active.id, requestSequence);
          setTerminalsByContext((current) => ({ ...current, [active.id]: next }));
          setError(null);
        }
        return next;
      } catch (cause) {
        const nextError = cause instanceof Error ? cause : new Error(String(cause));
        if (
          activeStorageKeyRef.current === operationStorageKey &&
          !tombstonesRef.current.has(active.id) &&
          (terminalMutationGenerationRef.current.get(active.id) ?? 0) === mutationGeneration &&
          requestSequence > (terminalAppliedRequestRef.current.get(active.id) ?? 0)
        ) {
          terminalAppliedRequestRef.current.set(active.id, requestSequence);
          setError(nextError);
        }
        throw nextError;
      }
    },
    [],
  );

  const createTerminal = useCallback(
    async (targetContext?: DraftWorkspaceContext): Promise<DraftTerminalInfo> => {
      const active = targetContext ?? contextRef.current;
      if (active === null) throw noActiveContextError();
      const operationStorageKey = activeStorageKeyRef.current;
      const operationId = randomUUID();
      setPendingTerminalCreates((current) => new Set(current).add(operationId));
      try {
        const terminal = await createDraftTerminal(active.hostId, active.id);
        if (
          activeStorageKeyRef.current !== operationStorageKey ||
          tombstonesRef.current.has(active.id)
        ) {
          await deleteDraftTerminal(active.hostId, active.id, terminal.id).catch(() => undefined);
          await discardDraftWorkspaceContext(active.hostId, active.id).catch(() => undefined);
          throw new Error("Draft workspace selection changed");
        }
        terminalMutationGenerationRef.current.set(
          active.id,
          (terminalMutationGenerationRef.current.get(active.id) ?? 0) + 1,
        );
        setTerminalsByContext((current) => {
          const terminals = current[active.id] ?? [];
          return terminals.some((row) => row.id === terminal.id)
            ? current
            : { ...current, [active.id]: [...terminals, terminal] };
        });
        setError(null);
        return terminal;
      } finally {
        setPendingTerminalCreates((current) => {
          if (!current.has(operationId)) return current;
          const next = new Set(current);
          next.delete(operationId);
          return next;
        });
      }
    },
    [],
  );

  const deleteTerminal = useCallback(
    async (terminalId: string, targetContext?: DraftWorkspaceContext): Promise<void> => {
      const active = targetContext ?? contextRef.current;
      if (active === null) throw noActiveContextError();
      const operationStorageKey = activeStorageKeyRef.current;
      await deleteDraftTerminal(active.hostId, active.id, terminalId);
      if (
        activeStorageKeyRef.current !== operationStorageKey ||
        tombstonesRef.current.has(active.id)
      ) {
        return;
      }
      terminalMutationGenerationRef.current.set(
        active.id,
        (terminalMutationGenerationRef.current.get(active.id) ?? 0) + 1,
      );
      setTerminalsByContext((current) => ({
        ...current,
        [active.id]: (current[active.id] ?? []).filter((row) => row.id !== terminalId),
      }));
      setError(null);
    },
    [],
  );

  const discard = useCallback(
    (targetContext?: DraftWorkspaceContext | null): Promise<void> => {
      if (targetContext === null) return Promise.resolve();
      const active = targetContext ?? contextRef.current;
      const currentDraft = contextsRef.current.find((row) => row.session_id === null) ?? null;
      if (targetContext === undefined || currentDraft?.id === active?.id) {
        transitionGenerationRef.current += 1;
      }
      if (active === null) return Promise.resolve();
      const flight = discardContext(active);
      transitionTailRef.current = Promise.all([transitionTailRef.current, flight]).then(
        () => undefined,
        () => undefined,
      );
      void flight.then(
        () => setError(null),
        (cause: unknown) => setError(cause instanceof Error ? cause : new Error(String(cause))),
      );
      return flight;
    },
    [discardContext],
  );

  const adopt = useCallback(
    async (
      adoptSessionId: string,
      targetContext?: DraftWorkspaceContext,
    ): Promise<DraftWorkspaceContext> => {
      const active = targetContext ?? contextRef.current;
      if (active === null) throw noActiveContextError();
      const adopted = await adoptDraftWorkspaceContext(active, adoptSessionId);
      if (!upsertContext(adopted)) throw new Error("Draft workspace selection changed");
      return adopted;
    },
    [upsertContext],
  );

  useEffect(() => {
    if (context === null) return;
    void refreshTerminals(context).catch(() => undefined);
    const timer = window.setInterval(() => {
      void refreshTerminals(context).catch(() => undefined);
    }, DRAFT_TERMINALS_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [context, refreshTerminals]);

  useEffect(() => {
    if (!scopeMatches) return;
    const heartbeatAll = async () => {
      const rows = readPersistedContexts(storageKey);
      await Promise.all(
        rows.map(async (row) => {
          try {
            const refreshed = await heartbeatDraftWorkspaceContext(restoredContext(row));
            // A handoff may complete while this request is in flight. Never let
            // its older ownership snapshot replace the adopted context.
            const current = contextsRef.current.find((stored) => stored.id === row.id);
            if (current !== undefined && current.session_id !== row.sessionId) return;
            upsertContext(refreshed);
          } catch (cause) {
            if (cause instanceof DraftWorkspaceHttpError && cause.status === 404) {
              clearContext(row.id);
            }
          }
        }),
      );
    };
    void heartbeatAll();
    const timer = window.setInterval(
      () => void heartbeatAll(),
      DRAFT_WORKSPACE_HEARTBEAT_INTERVAL_MS,
    );
    return () => window.clearInterval(timer);
  }, [clearContext, scopeMatches, storageKey, upsertContext]);

  return {
    context,
    terminals: context === null ? [] : (terminalsByContext[context.id] ?? []),
    isLoading:
      scopeMatches && (isLoading || pendingTerminalCreates.size > 0 || !terminalInventoryLoaded),
    error: scopeMatches ? error : null,
    ensureContext,
    refreshTerminals,
    createTerminal,
    deleteTerminal,
    discard,
    adopt,
  };
}
