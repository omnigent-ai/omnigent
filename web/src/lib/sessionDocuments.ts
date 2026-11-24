import { getBasePath } from "./basePath";
import { classifyGoogleDocsUrl } from "./googleDocs";
import { getOmnigentHostConfig, getOmnigentServerIdentity } from "./host";
import { authenticatedFetch, getCurrentUserId } from "./identity";
import { ApiError, apiErrorFromResponse } from "./sessionsApi";

export interface SessionDocumentEntry {
  documentUrl: string;
  /** Server-generated Unix microseconds, safe to compare as a JavaScript integer. */
  addedAt: number;
}

export interface SessionDocuments {
  sessionId: string;
  entries: SessionDocumentEntry[];
}

export interface SessionDocumentsRequest {
  readonly sessionId: string;
  readonly scope: string;
  readonly generation: number;
  readonly result: Promise<SessionDocuments>;
  /** Check before applying a result to the active page. Load again on session activation. */
  isCurrent: (activeSessionId: string) => boolean;
  /** Retain failed mutations: retry reconciles unknown outcomes without replaying them. */
  retry: () => SessionDocumentsRequest;
}

export function sessionDocumentsScope(sessionId: string): string {
  const server = getOmnigentServerIdentity();
  if (server === null) throw new Error("A backend identity is required for session documents.");
  return JSON.stringify([server, getBasePath(), getCurrentUserId(), sessionId]);
}

const pendingByScope = new Map<string, Promise<void>>();
const latestByScope = new Map<string, number>();
let generation = 0;

function documentId(url: string): string {
  const link = classifyGoogleDocsUrl(url);
  if (link.kind !== "embed") throw new Error("Expected a supported Google Docs editor URL.");
  return link.reference.documentId;
}

export function orderedSessionDocuments(
  entries: readonly SessionDocumentEntry[],
): SessionDocumentEntry[] {
  return [...entries].sort((left, right) => {
    const a = documentId(left.documentUrl);
    const b = documentId(right.documentUrl);
    return right.addedAt - left.addedAt || (a < b ? -1 : a > b ? 1 : 0);
  });
}

type Send = (suffix?: string, init?: RequestInit) => Promise<SessionDocuments>;

function createRequest(
  sessionId: string,
  operation: (send: Send) => Promise<SessionDocuments>,
): SessionDocumentsRequest {
  const scope = sessionDocumentsScope(sessionId);
  const fetcher = getOmnigentHostConfig().fetcher;
  const stillConnected = () =>
    getOmnigentServerIdentity() !== null &&
    scope === sessionDocumentsScope(sessionId) &&
    fetcher === getOmnigentHostConfig().fetcher;
  const requireConnected = () => {
    if (!stillConnected()) throw new Error("The connected user or backend changed.");
  };
  const send: Send = async (suffix = "", init) => {
    requireConnected();
    const res = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(sessionId)}/session-documents${suffix}`,
      { ...init, cache: "no-store" },
      requireConnected,
    );
    requireConnected();
    if (!res.ok) throw await apiErrorFromResponse(res);
    const body: unknown = await res.json();
    requireConnected();
    if (
      typeof body !== "object" ||
      body === null ||
      !("session_id" in body) ||
      body.session_id !== sessionId ||
      !("entries" in body) ||
      !Array.isArray(body.entries)
    )
      throw new Error("Wrong session document collection returned.");
    const ids = new Set<string>();
    const entries = body.entries.map((entry: unknown) => {
      if (
        typeof entry !== "object" ||
        entry === null ||
        !("document_url" in entry) ||
        typeof entry.document_url !== "string" ||
        entry.document_url.length > 2048 ||
        !("added_at" in entry) ||
        typeof entry.added_at !== "number" ||
        !Number.isSafeInteger(entry.added_at) ||
        entry.added_at <= 0
      )
        throw new Error("Invalid session document entry returned.");
      const id = documentId(entry.document_url);
      if (ids.has(id)) throw new Error("Duplicate session document identity returned.");
      ids.add(id);
      return { documentUrl: entry.document_url, addedAt: entry.added_at };
    });
    if (entries.length > 50) throw new Error("Invalid session document collection returned.");
    return { sessionId, entries: orderedSessionDocuments(entries) };
  };
  const enqueue = (): SessionDocumentsRequest => {
    const requestGeneration = ++generation;
    latestByScope.delete(scope);
    latestByScope.set(scope, requestGeneration);
    // Eviction makes old requests stale; it never makes an old request current.
    if (latestByScope.size > 1000) latestByScope.delete(latestByScope.keys().next().value!);
    const result = (pendingByScope.get(scope) ?? Promise.resolve()).then(() => {
      requireConnected();
      return operation(send);
    });
    const tail = result
      .then(
        () => {},
        () => {},
      )
      .finally(() => {
        if (pendingByScope.get(scope) === tail) pendingByScope.delete(scope);
      });
    pendingByScope.set(scope, tail);
    return {
      sessionId,
      scope,
      generation: requestGeneration,
      result,
      isCurrent: (activeSessionId) =>
        activeSessionId === sessionId &&
        stillConnected() &&
        latestByScope.get(scope) === requestGeneration,
      retry: enqueue,
    };
  };
  return enqueue();
}

export function loadSessionDocuments(sessionId: string): SessionDocumentsRequest {
  return createRequest(sessionId, (send) => send());
}

function definitelyRejected(error: unknown): boolean {
  return (
    error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408
  );
}

/** Call after explicit add confirmation. Navigate only after result resolves and isCurrent holds. */
export function addSessionDocument(sessionId: string, url: string): SessionDocumentsRequest {
  const id = documentId(url);
  const originalUrl = url.trim();
  if (originalUrl.length > 2048) throw new Error("Google Docs URL exceeds 2048 characters.");
  let sent = false;
  const confirmed = (collection: SessionDocuments) =>
    collection.entries.some((entry) => documentId(entry.documentUrl) === id);
  return createRequest(sessionId, async (send) => {
    if (sent) {
      const collection = await send();
      if (!confirmed(collection))
        throw new Error(
          "The add could not be confirmed or the document was removed. Reload before explicitly adding again.",
        );
      return collection;
    }
    sent = true;
    try {
      const collection = await send("", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: originalUrl }),
      });
      if (!confirmed(collection)) throw new Error("Could not confirm the document was linked.");
      return collection;
    } catch (error) {
      if (definitelyRejected(error)) sent = false;
      throw error;
    }
  });
}

export function removeSessionDocument(
  sessionId: string,
  entry: SessionDocumentEntry,
): SessionDocumentsRequest {
  const id = documentId(entry.documentUrl);
  let sent = false;
  return createRequest(sessionId, async (send) => {
    if (sent) {
      const collection = await send();
      if (collection.entries.some((candidate) => documentId(candidate.documentUrl) === id))
        throw new Error(
          "The removal could not be confirmed or the document was added again. Reload before explicitly removing again.",
        );
      return collection;
    }
    sent = true;
    try {
      const collection = await send(`/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (collection.entries.some((candidate) => documentId(candidate.documentUrl) === id))
        throw new Error("Could not confirm document removal.");
      return collection;
    } catch (error) {
      if (definitelyRejected(error)) sent = false;
      throw error;
    }
  });
}
