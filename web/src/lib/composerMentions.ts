import type { ComposerAttachment } from "@/store/chatStore";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";

/**
 * Pure ``@``-mention utilities (workspace files and other sessions) shared by the in-session composer
 * (``ChatPage``) and the new-session launcher (``NewChatDialog``). Kept free of
 * React/state so both surfaces parse, serialize, and mark up tagged paths
 * identically — and so the trigger logic is unit-testable without rendering.
 */

/** An active ``@``-file-mention being typed in a composer. */
export interface MentionState {
  /**
   * Text typed after the ``@`` (no whitespace), which doubles as a path:
   * the part up to the last ``/`` is the directory being browsed, the rest
   * filters that directory's entries. E.g. ``"src/fo"`` browses ``src`` and
   * filters by ``"fo"``; ``"src/"`` browses ``src`` with no filter.
   */
  query: string;
  /** Index of the ``@`` character in the textarea value. */
  start: number;
  /** Caret index (one past the last query char) — end of the token. */
  end: number;
}

/**
 * A workspace path tagged in a composer — via the ``@``-mention menu
 * (file/folder) or the file viewer's "Attach to agent" button (line range).
 * Structurally identical to the store's queued attachment, so it is the same
 * type: a chip drained from ``pendingComposerAttachments`` is a ``MentionItem``
 * unchanged.
 */
export type MentionItem = ComposerAttachment;

/** Serialize a tagged item to the path string that goes inside its marker. */
export function mentionItemPath(item: MentionItem): string {
  if (item.lineRange) return `${item.path}:${item.lineRange.start}-${item.lineRange.end}`;
  return item.isDir ? `${item.path}/` : item.path;
}

// Token preceding the caret: an "@" at the start of the inspected string or
// after whitespace, followed by a run with no whitespace and no further "@".
// (``^`` anchors to the start of the sliced ``before`` text, not a line — a
// mid-string "@" still matches because the newline before it counts as the
// ``\s``.) Mirrors the terminal FileMentionCompleter's "@"-trigger so the web
// behaves the same.
const MENTION_RE = /(?:^|\s)@([^\s@]*)$/;

// Same token, but its run may contain single spaces (session titles do).
const SPACED_MENTION_RE = /(?:^|\s)@([^\s@]+(?: [^\s@]+)* ?)$/;
/** Longest spaced ``@`` query kept open, in characters. */
const SPACED_MENTION_MAX = 80;

/**
 * Detect an in-progress ``@``-mention immediately before the caret.
 *
 * Looks only at ``text`` up to ``caret``. A space normally finishes the token
 * and closes the menu; it continues the token only while one of
 * ``sessionTitles`` (the sessions the menu lists now) still contains the
 * query, so ``@Casual g`` keeps finding "Casual greeting".
 *
 * :param text: The full textarea value.
 * :param caret: The caret offset (``selectionStart``).
 * :param sessionTitles: Titles the menu currently lists, e.g. ``["Casual greeting"]``.
 * :returns: The active :class:`MentionState`, or ``null``.
 */
export function detectMentionAt(
  text: string,
  caret: number,
  sessionTitles: readonly string[] = [],
): MentionState | null {
  const before = text.slice(0, caret);
  const m = MENTION_RE.exec(before) ?? spacedMention(before, sessionTitles);
  if (!m) return null;
  const query = m[1];
  // ``m.index`` points at the matched whitespace (or -1+1=0 at line start);
  // the "@" sits just before the captured query.
  const start = caret - query.length - 1;
  return { query, start, end: caret };
}

function spacedMention(before: string, sessionTitles: readonly string[]): RegExpExecArray | null {
  const m = SPACED_MENTION_RE.exec(before);
  if (!m || m[1].length > SPACED_MENTION_MAX) return null;
  const needle = m[1].toLowerCase();
  return sessionTitles.some((title) => title.toLowerCase().includes(needle)) ? m : null;
}

/**
 * Build the attachment marker for an "@"-tagged workspace ``path``, in the
 * wording the given native harness's executor uses for file delivery.
 *
 * Claude / pi / cursor executors emit ``[Attached: <path>]``; codex emits
 * ``[Attached file: <path>]`` (``codex_native_executor.py``). Both forms are
 * stripped from seeded titles by ``_ATTACHMENT_MARKER_RE``, but matching the
 * vendor's own wording keeps the marker consistent with what codex echoes
 * back in its mirrored transcript.
 *
 * Resolves the harness through ``nativeCodingAgentForHarness`` so reversed
 * spellings (``native-codex``) canonicalize to the same wording as
 * ``codex-native`` rather than silently falling through to the default.
 *
 * :param harness: The session harness, e.g. ``"codex-native"``.
 * :param path: Workspace-relative file path.
 * :returns: A single-line ``[Attached…: <path>]`` marker.
 */
export function mentionMarkerFor(harness: string | null, path: string): string {
  const isCodex = nativeCodingAgentForHarness(harness)?.key === "codex";
  return isCodex ? `[Attached file: ${path}]` : `[Attached: ${path}]`;
}

/** Default cap on how many mention rows the menu renders for one directory. */
export const MENTION_MATCH_CAP = 50;

/**
 * Split a mention query into the directory being browsed and the filter typed
 * within it. ``"src/fo"`` → browse ``"src"``, filter ``"fo"``; ``"src/"`` →
 * browse ``"src"``, no filter; ``"fo"`` → browse root (``""``), filter ``"fo"``.
 */
export function parseMentionToken(query: string): { dir: string; filter: string } {
  const slash = query.lastIndexOf("/");
  return slash >= 0
    ? { dir: query.slice(0, slash), filter: query.slice(slash + 1) }
    : { dir: "", filter: query };
}

/**
 * Filter a directory's entries by the typed segment, sort folders-first then
 * alphabetically, and cap the list. Generic over any ``{ name, type }`` row so
 * both the workspace-API and host-filesystem sources share one ranking. The cap
 * trims only the rendered *list*, never what gets delivered.
 */
export function rankMentionEntries<T extends { name: string; type: string }>(
  entries: T[],
  filter: string,
  cap: number = MENTION_MATCH_CAP,
): T[] {
  const needle = filter.toLowerCase();
  return entries
    .filter((e) => e.name.toLowerCase().includes(needle))
    .sort((a, b) => {
      if (a.type !== b.type) return a.type === "directory" ? -1 : 1;
      return a.name.localeCompare(b.name);
    })
    .slice(0, cap);
}

/** Another Omnigent session tagged through the composer's ``@`` menu. */
export interface SessionMention {
  /** Session id the agent passes to the ``sys_session_*`` tools. */
  id: string;
  /** Display title; ``"Untitled session"`` when the session has none. */
  title: string;
  /** True for a sub-agent spawned by the current session. */
  isSubagent: boolean;
  /** Composer token once tagged, e.g. ``"@Fix CI"``; see :func:`sessionMentionToken`. */
  token?: string;
}

/**
 * One line telling the agent how to act on tagged sessions. ``sys_session_send``
 * exists only for agents that can spawn, hence the fallback. Parsers key on the
 * ``[Tagged sessions:`` prefix only, so the wording can be tuned freely. It
 * rides in the message because native TUI harnesses never see framework prompt
 * text. Keep in sync with ``_SESSION_MENTION_HINT_RE`` in
 * ``omnigent/entities/conversation.py``.
 */
export const SESSION_MENTION_HINT =
  "[Tagged sessions: read with sys_session_get_history; to message one, use " +
  "sys_session_send if it is your own subagent and that tool is available, " +
  "otherwise sys_session_message]";

// Characters that would break the bracketed, quoted marker shape.
const SESSION_TITLE_UNSAFE_RE = /["[\]\r\n]+/g;

function cleanSessionTitle(title: string): string {
  return title.replace(SESSION_TITLE_UNSAFE_RE, " ").replace(/\s+/g, " ").trim();
}

/** Inline marker for one tagged session, e.g. ``[Session: "Fix CI" id=abc subagent=no]``. */
export function sessionMentionMarker(session: SessionMention): string {
  const title = cleanSessionTitle(session.title);
  return `[Session: "${title}" id=${session.id} subagent=${session.isSubagent ? "yes" : "no"}]`;
}

/**
 * ``base`` if no tagged item uses it yet, else ``base (suffix)`` with the first
 * free suffix, falling back to a counter. Keeps each token mapped to one target.
 */
export function uniqueMentionToken(
  base: string,
  taken: readonly string[],
  suffixes: readonly string[],
): string {
  if (!taken.includes(base)) return base;
  for (const suffix of suffixes) {
    const candidate = `${base} (${suffix})`;
    if (!taken.includes(candidate)) return candidate;
  }
  for (let n = 2; ; n++) {
    const candidate = `${base} (${n})`;
    if (!taken.includes(candidate)) return candidate;
  }
}

/** Composer token for a tagged file or folder, e.g. ``@src/``, ``@a.ts:3-9``. */
export function fileMentionToken(item: MentionItem): string {
  return item.token ?? `@${mentionItemPath(item)}`;
}

/** A new file tag's token, suffixed ``(file)`` when a tagged session owns the plain one. */
export function newFileMentionToken(item: MentionItem, taken: readonly string[]): string {
  return uniqueMentionToken(`@${mentionItemPath(item)}`, taken, ["file"]);
}

/**
 * Composer token for a tagged session, e.g. ``@Fix CI``. A token another tagged
 * item already uses gets an id-prefix suffix, lengthened until it is free.
 */
export function sessionMentionToken(session: SessionMention, taken: readonly string[]): string {
  const base = `@${cleanSessionTitle(session.title) || "Untitled session"}`;
  const { id } = session;
  return uniqueMentionToken(base, taken, [id.slice(0, 6), id.slice(0, 12), id]);
}

/** A token in composer text and the marker it becomes when the message is sent. */
export interface InlineMention {
  token: string;
  marker: string;
}

/** Tokens and markers for everything tagged in a composer. */
export function composerInlineMentions(
  items: readonly MentionItem[],
  sessions: readonly SessionMention[],
  harness: string | null,
): InlineMention[] {
  return [
    ...items.map((item) => ({
      token: fileMentionToken(item),
      marker: mentionMarkerFor(harness, mentionItemPath(item)),
    })),
    ...sessions.map((session) => ({
      token: session.token ?? `@${cleanSessionTitle(session.title)}`,
      marker: sessionMentionMarker(session),
    })),
  ];
}

// A token must start a word and end at whitespace, punctuation or the text end.
const TOKEN_END_RE = /[\s.,;:!?)\]}'"]/;

/** Where a known token sits in composer text. */
export interface MentionTokenRange {
  start: number;
  end: number;
  token: string;
}

/**
 * Non-overlapping occurrences of ``tokens`` in ``text``, in text order. Longer
 * tokens win, so ``@src/a.ts:3-9`` is not read as ``@src/a.ts``.
 */
export function findMentionTokens(text: string, tokens: readonly string[]): MentionTokenRange[] {
  const ranges: MentionTokenRange[] = [];
  const byLength = [...new Set(tokens)].filter(Boolean).sort((a, b) => b.length - a.length);
  for (const token of byLength) {
    for (let at = text.indexOf(token); at >= 0; at = text.indexOf(token, at + 1)) {
      const end = at + token.length;
      if (at > 0 && !/\s/.test(text[at - 1])) continue;
      if (end < text.length && !TOKEN_END_RE.test(text[end])) continue;
      if (ranges.some((r) => at < r.end && end > r.start)) continue;
      ranges.push({ start: at, end, token });
    }
  }
  return ranges.sort((a, b) => a.start - b.start);
}

/** Token the caret is at the end of, or inside, so Backspace removes it whole. */
export function mentionTokenAtCaret(
  text: string,
  caret: number,
  tokens: readonly string[],
): MentionTokenRange | null {
  return findMentionTokens(text, tokens).find((r) => caret > r.start && caret <= r.end) ?? null;
}

/** Mentions whose token still appears in any of ``texts``; deleted tokens drop out. */
export function presentMentions<T extends InlineMention>(
  mentions: readonly T[],
  texts: readonly string[],
): T[] {
  // Scan all tokens together so a token found only inside a longer one
  // (``@README.md`` within ``@README.md (file)``) does not count as present.
  const tokens = mentions.map((m) => m.token);
  const found = new Set(texts.flatMap((t) => findMentionTokens(t, tokens).map((r) => r.token)));
  return mentions.filter((m) => found.has(m.token));
}

/** Replace each token in ``text`` with its marker, in place. */
export function expandMentionTokens(text: string, mentions: readonly InlineMention[]): string {
  const markerFor = new Map(mentions.map((m) => [m.token, m.marker]));
  let out = text;
  for (const range of findMentionTokens(text, [...markerFor.keys()]).reverse()) {
    out = out.slice(0, range.start) + markerFor.get(range.token) + out.slice(range.end);
  }
  return out;
}

/**
 * Append the tool hint once when the message tags any session. A restored
 * message that already ends with the hint keeps a single copy.
 */
export function withSessionMentionHint(text: string, tagsSessions: boolean): string {
  if (!tagsSessions) return text;
  return `${stripTrailingHint(text) ?? text.trimEnd()}\n\n${SESSION_MENTION_HINT}`;
}

/** A mention found in a sent message. */
export type SentMention =
  | { kind: "session"; id: string; title: string; isSubagent: boolean }
  | { kind: "file"; path: string };

// An absolute filesystem path (POSIX, Windows drive or UNC). Composer file
// mentions are workspace-relative, so an absolute marker is a native executor's
// materialized upload, which the bubble already shows as a file block.
export function isAbsolutePath(path: string): boolean {
  return /^(\/|[A-Za-z]:[\\/]|\\\\)/.test(path);
}

// Linear on hostile input: no group rescans the rest of the message after an
// unclosed "[", and the trailing hint is found with string search.
const SENT_SESSION_RE = /\[Session: "([^"\]]*)" id=([A-Za-z0-9_:-]+) subagent=(yes|no)\]/g;
const SENT_ATTACHED_RE = /\[Attached(?: file)?:([^\][]*)\]([ \t]*(?:\r?\n)*)/g;
const SENT_HINT_PREFIX = "[Tagged sessions:";

/** ``text`` without its trailing tool hint, or ``null`` when it has none. */
function stripTrailingHint(text: string): string | null {
  // The hint is only ever appended at the very end; elsewhere it is user text.
  const end = text.trimEnd();
  const start = end.lastIndexOf(SENT_HINT_PREFIX);
  if (start < 0 || end.indexOf("]", start) !== end.length - 1) return null;
  return end.slice(0, start).trimEnd();
}

/**
 * Rewrite the markers in a sent message with ``render``: drops the trailing tool
 * hint, drops absolute-path upload markers, and passes each relative file marker
 * and, when the hint is present, each session marker to ``render``.
 */
export function replaceSentMentions(
  text: string,
  render: (mention: SentMention) => string,
): string {
  // The composer always ends a message that tags a session with the hint, so a
  // session marker in a message without it is text the user typed, not a tag.
  const withoutHint = stripTrailingHint(text);
  const withSessions =
    withoutHint === null
      ? text
      : withoutHint.replace(SENT_SESSION_RE, (_m, title: string, id: string, sub: string) =>
          render({ kind: "session", id, title, isSubagent: sub === "yes" }),
        );
  return withSessions.replace(SENT_ATTACHED_RE, (_m, raw: string, trail: string) => {
    const path = raw.trim();
    if (!path || isAbsolutePath(path)) return "";
    return render({ kind: "file", path }) + trail;
  });
}

/**
 * Turn a sent or queued message back into an editable draft: when it ends with
 * the generated hint (so its session markers are tags), drop the hint and
 * replace each session marker with a token, returning the tags. A message
 * without the hint is returned unchanged; its markers are the user's text.
 *
 * :param main: The segment that carries the hint, e.g. the draft's tail text.
 * :param others: Other editable segments that may hold session markers.
 * :returns: The rewritten segments, in order, and the recovered session tags.
 */
export function recallSessionMentions(
  main: string,
  others: readonly string[] = [],
): { main: string; others: string[]; sessions: SessionMention[] } {
  const withoutHint = stripTrailingHint(main);
  if (withoutHint === null) return { main, others: [...others], sessions: [] };
  const sessions: SessionMention[] = [];
  const toToken = (_m: string, title: string, id: string, sub: string) => {
    let session = sessions.find((it) => it.id === id);
    if (!session) {
      const found: SessionMention = { id, title, isSubagent: sub === "yes" };
      found.token = sessionMentionToken(found, sessions.map(sessionTokenOf));
      sessions.push(found);
      session = found;
    }
    return sessionTokenOf(session);
  };
  return {
    main: withoutHint.replace(SENT_SESSION_RE, toToken),
    others: others.map((segment) => segment.replace(SENT_SESSION_RE, toToken)),
    sessions,
  };
}

/** Sent-message text for reading and copying: markers become ``@name``. */
export function readableSentText(text: string): string {
  return replaceSentMentions(text, (m) => (m.kind === "session" ? `@${m.title}` : `@${m.path}`));
}

/** Everything tagged in one composer draft, saved and restored with its text. */
export interface TaggedMentions {
  files: MentionItem[];
  sessions: SessionMention[];
}

// A tagged file's identity: the same path, kind and line range.
function mentionItemIdentity(item: MentionItem): string {
  const range = item.lineRange ? `${item.lineRange.start}-${item.lineRange.end}` : "";
  return `${item.path}|${item.isDir}|${range}`;
}

/**
 * Combine two drafts' text and tags without letting a shared token retarget a
 * mention. A tag already in ``first`` keeps its token; a ``second`` tag reuses
 * it for the same target, or gets a new unique token (rewritten in
 * ``second.text``) when its token names a different target.
 *
 * :param first: The draft whose tokens win, e.g. the launcher draft.
 * :param second: The draft merged in, e.g. a failed temporary session's draft.
 * :returns: ``second.text`` with renamed tokens, and the combined tags.
 */
export function mergeTaggedDrafts(
  first: TaggedMentions | undefined,
  second: { text: string; mentions?: TaggedMentions },
): { text: string; mentions: TaggedMentions | undefined } {
  if (!second.mentions) return { text: second.text, mentions: first };
  const files = [...(first?.files ?? [])];
  const sessions = [...(first?.sessions ?? [])];
  const taken = [...files.map(fileMentionToken), ...sessions.map(sessionTokenOf)];
  const secondTokens = [
    ...second.mentions.files.map(fileMentionToken),
    ...second.mentions.sessions.map(sessionTokenOf),
  ];
  // A new token must not collide with a later tag of ``second`` either.
  const avoid = (own: string) => [...taken, ...secondTokens.filter((t) => t !== own)];
  const renamed = new Map<string, string>();
  const claim = (from: string, to: string) => {
    if (from !== to) renamed.set(from, to);
    if (!taken.includes(to)) taken.push(to);
  };
  for (const item of second.mentions.files) {
    const token = fileMentionToken(item);
    const same = files.find((it) => mentionItemIdentity(it) === mentionItemIdentity(item));
    if (same) {
      claim(token, fileMentionToken(same));
      continue;
    }
    const next = taken.includes(token) ? newFileMentionToken(item, avoid(token)) : token;
    files.push({ ...item, token: next });
    claim(token, next);
  }
  for (const session of second.mentions.sessions) {
    const token = sessionTokenOf(session);
    const same = sessions.find((it) => it.id === session.id);
    if (same) {
      claim(token, sessionTokenOf(same));
      continue;
    }
    const next = taken.includes(token) ? sessionMentionToken(session, avoid(token)) : token;
    sessions.push({ ...session, token: next });
    claim(token, next);
  }
  // Scan every token of ``second`` so a longer one is never split by a shorter rename.
  let text = second.text;
  for (const range of findMentionTokens(text, secondTokens).reverse()) {
    const next = renamed.get(range.token);
    if (next !== undefined) text = text.slice(0, range.start) + next + text.slice(range.end);
  }
  return { text, mentions: { files, sessions } };
}

function sessionTokenOf(session: SessionMention): string {
  return session.token ?? `@${session.title}`;
}

function readTaggedFile(value: unknown): MentionItem | null {
  if (typeof value !== "object" || value === null) return null;
  const { path, isDir, lineRange, token } = value as Record<string, unknown>;
  if (typeof path !== "string" || typeof isDir !== "boolean") return null;
  const item: MentionItem = { path, isDir };
  if (typeof token === "string") item.token = token;
  if (typeof lineRange === "object" && lineRange !== null) {
    const { start, end } = lineRange as Record<string, unknown>;
    if (typeof start === "number" && typeof end === "number") item.lineRange = { start, end };
  }
  return item;
}

function readTaggedSession(value: unknown): SessionMention | null {
  if (typeof value !== "object" || value === null) return null;
  const { id, title, isSubagent, token } = value as Record<string, unknown>;
  if (typeof id !== "string" || typeof title !== "string" || typeof isSubagent !== "boolean")
    return null;
  return typeof token === "string" ? { id, title, isSubagent, token } : { id, title, isSubagent };
}

/** Tagged items read back from storage; invalid entries are dropped. */
export function readTaggedMentions(value: unknown): TaggedMentions | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const { files, sessions } = value as Record<string, unknown>;
  const result: TaggedMentions = {
    files: Array.isArray(files) ? files.map(readTaggedFile).filter((it) => it !== null) : [],
    sessions: Array.isArray(sessions)
      ? sessions.map(readTaggedSession).filter((it) => it !== null)
      : [],
  };
  return result.files.length > 0 || result.sessions.length > 0 ? result : undefined;
}

/** Shared empty list, so default props keep a stable reference. */
export const NO_SESSION_MENTIONS: readonly SessionMention[] = [];

/** Default cap on session rows in the ``@`` menu. */
export const SESSION_MENTION_CAP = 8;

/**
 * Filter candidate sessions by a case-insensitive title substring, keeping
 * caller order (subagents and recency first), dropping duplicate ids and
 * capping the list. Path-shaped queries (containing ``/``) match no session.
 */
export function rankSessionMentions(
  candidates: SessionMention[],
  query: string,
  cap: number = SESSION_MENTION_CAP,
): SessionMention[] {
  if (query.includes("/")) return [];
  const needle = query.toLowerCase();
  const seen = new Set<string>();
  const out: SessionMention[] = [];
  for (const candidate of candidates) {
    if (seen.has(candidate.id) || !candidate.title.toLowerCase().includes(needle)) continue;
    seen.add(candidate.id);
    out.push(candidate);
    if (out.length >= cap) break;
  }
  return out;
}
