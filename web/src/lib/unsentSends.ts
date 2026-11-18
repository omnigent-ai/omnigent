// Sends this tab hasn't seen answered, and queued messages, per session in sessionStorage.
// The server can hold a POST for minutes (a sandbox launch or wake) without persisting
// it, and the queue lives in memory, so a reload would otherwise lose the text.
import { readComposerDraft, type StoredReplyDraft } from "./replyDraft";

export interface UnsentSend {
  stableId: string;
  text: string;
  replyDraft?: StoredReplyDraft;
  /** The sender (`getCurrentUserId`), so another account in this tab can't recover it. */
  authorId: string;
}

const UNSENT_SENDS_KEY = "omnigent.unsentSends";
const QUEUED_SENDS_KEY = "omnigent.queuedSends";

// A page that is going away aborts its open sends, and their cleanup still runs
// as it unloads; keep their records for the next page instead.
let pageHiding = false;
if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => {
    pageHiding = true;
  });
  window.addEventListener("pageshow", () => {
    pageHiding = false;
  });
}

function parseSend(entry: unknown): UnsentSend | null {
  const draft = readComposerDraft(entry);
  if (!draft?.text || typeof entry !== "object" || entry === null) return null;
  const { stableId, authorId } = entry as { stableId?: unknown; authorId?: unknown };
  // A record with no sender can't be matched to the viewer.
  if (typeof stableId !== "string" || typeof authorId !== "string") return null;
  return { ...draft, stableId, authorId };
}

function parseQueue(entry: unknown): UnsentSend[] | null {
  const sends = Array.isArray(entry) ? entry.map(parseSend).filter((send) => send !== null) : [];
  return sends.length > 0 ? sends : null;
}

function read<T>(key: string, parse: (entry: unknown) => T | null): Record<string, T> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.sessionStorage.getItem(key);
    const entries: unknown = raw ? JSON.parse(raw) : {};
    if (typeof entries !== "object" || entries === null || Array.isArray(entries)) return {};
    const records: Record<string, T> = {};
    for (const [conversationId, entry] of Object.entries(entries)) {
      const record = parse(entry);
      if (record !== null) records[conversationId] = record;
    }
    return records;
  } catch {
    return {};
  }
}

function write(records: Record<string, unknown>, key: string): void {
  if (typeof window === "undefined") return;
  try {
    if (Object.keys(records).length === 0) window.sessionStorage.removeItem(key);
    else window.sessionStorage.setItem(key, JSON.stringify(records));
  } catch {
    // Storage full or unavailable: a reload mid-send loses the text, as before.
  }
}

/** Record a message about to be POSTed; the latest send per session wins. */
export function rememberUnsentSend(conversationId: string, send: UnsentSend): void {
  write({ ...read(UNSENT_SENDS_KEY, parseSend), [conversationId]: send }, UNSENT_SENDS_KEY);
}

/** Drop a session's record once this tab has seen that send settle. */
export function forgetUnsentSend(conversationId: string, stableId: string): void {
  if (pageHiding) return;
  const sends = read(UNSENT_SENDS_KEY, parseSend);
  if (sends[conversationId]?.stableId !== stableId) return;
  const others = Object.entries(sends).filter(([id]) => id !== conversationId);
  write(Object.fromEntries(others), UNSENT_SENDS_KEY);
}

/** The send this tab last left unanswered in a session, if any. */
export function peekUnsentSend(conversationId: string): UnsentSend | null {
  return read(UNSENT_SENDS_KEY, parseSend)[conversationId] ?? null;
}

/** Record the messages a session's queue holds, in order; an empty list clears it. */
export function rememberQueuedSends(conversationId: string, sends: UnsentSend[]): void {
  if (pageHiding) return;
  const queues = Object.entries(read(QUEUED_SENDS_KEY, parseQueue));
  const others = queues.filter(([id]) => id !== conversationId);
  write(
    Object.fromEntries(sends.length > 0 ? [...others, [conversationId, sends]] : others),
    QUEUED_SENDS_KEY,
  );
}

/** The messages kept for a session's queue, in order. */
export function peekQueuedSends(conversationId: string): UnsentSend[] {
  return read(QUEUED_SENDS_KEY, parseQueue)[conversationId] ?? [];
}

/** Clear every record, for isolated tests. */
export function clearUnsentSends(): void {
  write({}, UNSENT_SENDS_KEY);
  write({}, QUEUED_SENDS_KEY);
}
