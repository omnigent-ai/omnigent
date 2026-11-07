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

/**
 * Detect an in-progress ``@``-mention immediately before the caret.
 *
 * Looks only at ``text`` up to ``caret`` so a trailing space (token
 * finished) closes the menu. Returns ``null`` when there is no active
 * mention token.
 *
 * :param text: The full textarea value.
 * :param caret: The caret offset (``selectionStart``).
 * :returns: The active :class:`MentionState`, or ``null``.
 */
export function detectMentionAt(text: string, caret: number): MentionState | null {
  const before = text.slice(0, caret);
  const m = MENTION_RE.exec(before);
  if (!m) return null;
  const query = m[1];
  // ``m.index`` points at the matched whitespace (or -1+1=0 at line start);
  // the "@" sits just before the captured query.
  const start = caret - query.length - 1;
  return { query, start, end: caret };
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

/** Composer token for a tagged file or folder, e.g. ``@src/``, ``@a.ts:3-9``. */
export function fileMentionToken(item: MentionItem): string {
  return `@${mentionItemPath(item)}`;
}

/**
 * Composer token for a tagged session, e.g. ``@Fix CI``. A title already used
 * by another tagged token gets a short id suffix, so each token maps to one
 * session.
 */
export function sessionMentionToken(session: SessionMention, taken: readonly string[]): string {
  const base = `@${cleanSessionTitle(session.title) || "Untitled session"}`;
  return taken.includes(base) ? `${base} (${session.id.slice(0, 6)})` : base;
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
  return mentions.filter((m) => texts.some((t) => findMentionTokens(t, [m.token]).length > 0));
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

/** Append the tool hint once when the message tags any session. */
export function withSessionMentionHint(text: string, tagsSessions: boolean): string {
  return tagsSessions ? `${text.trimEnd()}\n\n${SESSION_MENTION_HINT}` : text;
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

const SENT_SESSION_RE = /\[Session: "([^"\]]*)" id=([A-Za-z0-9_:-]+) subagent=(yes|no)\]/g;
const SENT_ATTACHED_RE = /\[Attached(?: file)?:\s*([^\]]*)\]([ \t]*(?:\r?\n)*)/g;
// The hint is only ever appended at the very end; elsewhere it is user text.
const SENT_HINT_RE = /(?:\r?\n)*[ \t]*\[Tagged sessions:[^\]]*\]\s*$/;

/**
 * Rewrite the markers in a sent message with ``render``: drops the tool hint,
 * drops absolute-path upload markers, and passes each session or relative file
 * marker to ``render`` for its replacement text.
 */
export function replaceSentMentions(
  text: string,
  render: (mention: SentMention) => string,
): string {
  return text
    .replace(SENT_HINT_RE, "")
    .replace(SENT_SESSION_RE, (_m, title: string, id: string, sub: string) =>
      render({ kind: "session", id, title, isSubagent: sub === "yes" }),
    )
    .replace(SENT_ATTACHED_RE, (_m, raw: string, trail: string) => {
      const path = raw.trim();
      if (!path || isAbsolutePath(path)) return "";
      return render({ kind: "file", path }) + trail;
    });
}

/** Sent-message text for reading and copying: markers become ``@name``. */
export function readableSentText(text: string): string {
  return replaceSentMentions(text, (m) => (m.kind === "session" ? `@${m.title}` : `@${m.path}`));
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
