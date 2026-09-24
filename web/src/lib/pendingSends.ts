import type { MessageContentBlock } from "./blocks";

/**
 * A send that never got a 2xx (parked after its retries, or refused), kept
 * across a reload so the message is re-sent with its original stable id
 * instead of vanishing with the tab's memory. Per tab (`sessionStorage`), so
 * a second tab never re-sends another tab's messages.
 */
export interface PersistedPendingSend {
  stableId: string;
  content: MessageContentBlock[];
  createdAtS?: number;
  author?: string;
}

const STORAGE_KEY = "omnigent.pendingSends";
/**
 * Failed sends kept per conversation. Older ones fall off the reload record
 * (their text is still in the transcript until the tab closes); a user with
 * more than this many undelivered messages in one chat has a bigger problem
 * than the record.
 */
const MAX_PER_CONVERSATION = 20;

type Stored = Record<string, PersistedPendingSend[]>;

function isRecord(value: unknown): value is PersistedPendingSend {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.stableId === "string" &&
    /^[0-9a-f]{32}$/.test(record.stableId) &&
    Array.isArray(record.content)
  );
}

function load(): Stored {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    const stored: Stored = {};
    for (const [conversationId, records] of Object.entries(parsed)) {
      if (!Array.isArray(records)) continue;
      const valid = records.filter(isRecord);
      if (valid.length > 0) stored[conversationId] = valid;
    }
    return stored;
  } catch {
    return {};
  }
}

function save(stored: Stored): void {
  if (typeof window === "undefined") return;
  try {
    if (Object.keys(stored).length === 0) window.sessionStorage.removeItem(STORAGE_KEY);
    else window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
  } catch {
    // Storage full or unavailable — the send still lives in tab memory.
  }
}

export function readPendingSends(conversationId: string): PersistedPendingSend[] {
  return load()[conversationId] ?? [];
}

/** Remember a send; a record with the same stable id is replaced in place. */
export function persistPendingSend(conversationId: string, record: PersistedPendingSend): void {
  const stored = load();
  const kept = (stored[conversationId] ?? []).filter((r) => r.stableId !== record.stableId);
  stored[conversationId] = [...kept, record].slice(-MAX_PER_CONVERSATION);
  save(stored);
}

export function forgetPendingSend(conversationId: string, stableId: string): void {
  const stored = load();
  const records = stored[conversationId];
  if (records === undefined) return;
  const kept = records.filter((r) => r.stableId !== stableId);
  if (kept.length > 0) {
    save({ ...stored, [conversationId]: kept });
    return;
  }
  save(Object.fromEntries(Object.entries(stored).filter(([id]) => id !== conversationId)));
}
