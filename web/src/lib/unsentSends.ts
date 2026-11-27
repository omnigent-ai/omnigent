// Sends this tab hasn't seen answered, and queued messages, per session in sessionStorage.
// The server can hold a POST for minutes (a sandbox launch or wake) without persisting
// it, and the queue lives in memory, so a reload would otherwise lose the text.
import { isPageHiding } from "./pageLifecycle";
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

function parseSend(entry: unknown): UnsentSend | null {
  const draft = readComposerDraft(entry);
  if (!draft?.text || typeof entry !== "object" || entry === null) return null;
  const { stableId, authorId } = entry as { stableId?: unknown; authorId?: unknown };
  // A record with no sender can't be matched to the viewer.
  if (typeof stableId !== "string" || typeof authorId !== "string") return null;
  return { ...draft, stableId, authorId };
}

/**
 * A session's unsent-send records. Each session holds a list, one record per message. A record
 * written before the list existed is a single object, read as a one-message list.
 */
function parseUnsentList(entry: unknown): UnsentSend[] | null {
  const sends = (Array.isArray(entry) ? entry : [entry])
    .map(parseSend)
    .filter((send): send is UnsentSend => send !== null);
  return sends.length > 0 ? sends : null;
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

/** Record a message about to be POSTed. Each message keeps its own record, keyed by its id. */
export function rememberUnsentSend(conversationId: string, send: UnsentSend): void {
  const records = read(UNSENT_SENDS_KEY, parseUnsentList);
  const others = (records[conversationId] ?? []).filter((r) => r.stableId !== send.stableId);
  write({ ...records, [conversationId]: [...others, send] }, UNSENT_SENDS_KEY);
}

/** Drop one message's record once this tab has seen that send settle. Other records stay. */
export function forgetUnsentSend(conversationId: string, stableId: string): void {
  // A page going away aborts its open sends; their records stay for the next page.
  if (isPageHiding()) return;
  const records = read(UNSENT_SENDS_KEY, parseUnsentList);
  const left = (records[conversationId] ?? []).filter((r) => r.stableId !== stableId);
  if (left.length === (records[conversationId] ?? []).length) return;
  const next = Object.fromEntries(Object.entries(records).filter(([id]) => id !== conversationId));
  if (left.length > 0) next[conversationId] = left;
  write(next, UNSENT_SENDS_KEY);
}

/** Every unsent message a session's records hold, in the order they were sent. */
export function peekUnsentSends(conversationId: string): UnsentSend[] {
  return read(UNSENT_SENDS_KEY, parseUnsentList)[conversationId] ?? [];
}

/** The latest unsent message a session holds, if any. */
export function peekUnsentSend(conversationId: string): UnsentSend | null {
  const sends = peekUnsentSends(conversationId);
  return sends[sends.length - 1] ?? null;
}

/** Record the messages a session's queue holds, in order; an empty list clears it. */
export function rememberQueuedSends(conversationId: string, sends: UnsentSend[]): void {
  if (isPageHiding()) return;
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
