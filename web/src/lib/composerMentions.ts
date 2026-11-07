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

/**
 * Assemble the attachment preamble prepended to an outgoing message: one
 * harness-aware marker per tagged item, on its own line, terminated by a blank
 * line. Returns ``""`` when nothing is tagged.
 */
export function buildMentionPreamble(
  items: MentionItem[],
  harness: string | null,
  sessions: SessionMention[] = [],
): string {
  const lines = [
    ...sessions.map(sessionMentionMarker),
    ...(sessions.length > 0 ? [SESSION_MENTION_HINT] : []),
    ...items.map((it) => mentionMarkerFor(harness, mentionItemPath(it))),
  ];
  return lines.length === 0 ? "" : lines.join("\n") + "\n\n";
}

/** Another Omnigent session tagged through the composer's ``@`` menu. */
export interface SessionMention {
  /** Session id the agent passes to the ``sys_session_*`` tools. */
  id: string;
  /** Display title; ``"Untitled session"`` when the session has none. */
  title: string;
  /** True for a sub-agent spawned by the current session. */
  isSubagent: boolean;
}

/**
 * One line telling the agent how to act on tagged sessions. It rides in the
 * message itself because native TUI harnesses never see framework prompt
 * text. Keep in sync with ``_SESSION_MENTION_LINE_RE`` in
 * ``omnigent/entities/conversation.py``.
 */
export const SESSION_MENTION_HINT =
  "[Tagged sessions are Omnigent sessions: read them with sys_session_get_info and " +
  "sys_session_get_history; message a subagent with sys_session_send and any other " +
  "session with sys_session_message]";

// Characters that would break the bracketed, quoted marker shape.
const SESSION_TITLE_UNSAFE_RE = /["[\]\r\n]+/g;

/** Marker line for one tagged session, e.g. ``[Session: "Fix CI" id=abc subagent=no]``. */
export function sessionMentionMarker(session: SessionMention): string {
  const title = session.title.replace(SESSION_TITLE_UNSAFE_RE, " ").replace(/\s+/g, " ").trim();
  return `[Session: "${title}" id=${session.id} subagent=${session.isSubagent ? "yes" : "no"}]`;
}

// Parses the markers ``sessionMentionMarker`` emits (and the hint line), with
// their trailing line breaks, so a sent bubble can show chips instead.
const SESSION_MARKER_RE =
  /\[Session: "([^"\]]*)" id=([A-Za-z0-9_:-]+) subagent=(yes|no)\][ \t]*(?:\r?\n)*/g;
const SESSION_HINT_LINE_RE = /\[Tagged sessions are Omnigent sessions:[^\]]*\][ \t]*(?:\r?\n)*/g;

/** Sessions tagged in a sent message's text, in marker order. */
export function extractSessionMentions(text: string): SessionMention[] {
  return [...text.matchAll(SESSION_MARKER_RE)].map((m) => ({
    title: m[1],
    id: m[2],
    isSubagent: m[3] === "yes",
  }));
}

/** Remove session markers and the hint line from a sent message's text. */
export function stripSessionMentionMarkers(text: string): string {
  return text.replace(SESSION_MARKER_RE, "").replace(SESSION_HINT_LINE_RE, "");
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
