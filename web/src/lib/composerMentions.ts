import { type ComposerAttachment } from "@/store/chatStore";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";

/**
 * Pure ``@``-file-mention utilities shared by the in-session composer
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
export function buildMentionPreamble(items: MentionItem[], harness: string | null): string {
  if (items.length === 0) return "";
  return items.map((it) => mentionMarkerFor(harness, mentionItemPath(it))).join("\n") + "\n\n";
}
