import { type RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  historyDirectionOf,
  type ComposerInputEvent,
  type ComposerKeyboardEvent,
  type ComposerTextTarget,
} from "@/components/composer/engine/composerInput";
import { insertComposerText } from "@/lib/composerText";
import { attachmentFilename } from "@/lib/attachments";

import {
  detectMentionAt,
  fileMentionToken,
  findMentionTokens,
  mentionTokenAtCaret,
  newFileMentionToken,
  NO_SESSION_MENTIONS,
  SESSION_MENTION_CAP,
  type MentionItem,
  type MentionState,
  type PastedAttachment,
  type SessionMention,
  sessionMentionToken,
  type LiveMentions,
  type TaggedMentions,
  nameAttachmentFile,
  tagTokensOf,
} from "@/lib/composerMentions";
import { composerAttachmentKey } from "@/store/chatStore";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

const NO_FILE_ENTRIES: WorkspaceFile[] = [];
const NO_TAGS: LiveMentions = { files: [], sessions: [] };

function sessionTagToken(session: SessionMention): string {
  return session.token ?? `@${session.title}`;
}

const NO_ATTACHMENTS: PastedAttachment[] = [];

function attachmentsOf(tags: LiveMentions): readonly PastedAttachment[] {
  return tags.attachments ?? NO_ATTACHMENTS;
}

/**
 * Where a deleted tag's token was removed: the input, its text just before that change, and the
 * one history direction whose change may reclaim the tag.
 */
interface RestorePoint {
  input: ComposerTextTarget;
  value: string;
  reclaimedBy: "undo" | "redo";
}

/** The tokens of `tokens` that stand in any of `segments`. */
function tokensIn(segments: readonly string[], tokens: readonly string[]): Set<string> {
  return new Set(
    segments.flatMap((segment) => findMentionTokens(segment, tokens).map((range) => range.token)),
  );
}

/** `tags` without anything `removed` names by token. */
function withoutTags(tags: LiveMentions, removed: LiveMentions): Required<LiveMentions> {
  const gone = new Set(tagTokensOf(removed));
  return {
    files: tags.files.filter((it) => !gone.has(fileMentionToken(it))),
    sessions: tags.sessions.filter((it) => !gone.has(sessionTagToken(it))),
    attachments: attachmentsOf(tags).filter((it) => !gone.has(it.token)),
  };
}

/** `tags` plus `added`, which replace any with the same tokens. */
function mergeTags(tags: LiveMentions, added: LiveMentions): Required<LiveMentions> {
  const kept = withoutTags(tags, added);
  return {
    files: [...kept.files, ...added.files],
    sessions: [...kept.sessions, ...added.sessions],
    attachments: [...kept.attachments, ...attachmentsOf(added)],
  };
}

/**
 * Inputs the host composer supplies. The data source (workspace API vs. host
 * filesystem) and the mention-token state live in the composer — only the
 * stateful glue (selection index, tagged chips, attach/drill/remove handlers,
 * keyboard navigation, top-row preselect) is shared here, so the two composers
 * can't drift.
 */
export interface MentionBrowserParams {
  /** Active mention token, owned by the composer (recomputed on text change). */
  mention: MentionState | null;
  /** Clear or replace the active token (e.g. on attach, drill, or dismiss). */
  setMention: (next: MentionState | null) => void;
  /** Current directory's entries — already filtered, folders-first, capped. */
  mentionEntries: WorkspaceFile[];
  /** Every matching session, uncapped; the browser decides how many to show. */
  sessionEntries?: readonly SessionMention[];
  /** The textarea value and a setter (which may also flag the draft dirty). */
  text: string;
  setText: (next: string) => void;
  textareaRef: RefObject<ComposerTextTarget | null>;
  /** On mobile, Enter inserts a newline rather than acting on the menu. */
  isMobile?: boolean;
  /** Tags restored with a saved draft, used for the first render only. */
  initialMentions?: TaggedMentions;
  /**
   * Every editable text segment of the draft (defaults to ``[text]``). A tag
   * whose token is in none of them is dropped, so retyping its text later is
   * a literal, not the deleted tag.
   */
  segments?: readonly string[];
  /** Takes the Files of chips a reset drops, so they go out as tray files. */
  onDemoteChips?: (files: File[]) => void;
  /**
   * Tags the caller keeps (a grid cell's session draft) instead of this hook's
   * own state. The caller's draft drops a tag once its token leaves the text,
   * so the release pass skips file and session tags.
   */
  tags?: {
    mentions: TaggedMentions;
    /** Applies ``update`` to the latest stored tags, not the rendered ones. */
    updateMentions: (update: (current: TaggedMentions) => TaggedMentions) => void;
  };
}

function resolveUpdate<T>(update: React.SetStateAction<T>, current: T): T {
  return typeof update === "function" ? (update as (previous: T) => T)(current) : update;
}

/** An open level of the ``@`` session menu; ``null`` is the top level. */
export type SessionMenuLevel = "sessions" | "subagents";

/** What the ``@`` menu renders above the files. */
export interface SessionMenuView {
  /** Session rows to render, indexed first (after the category row, if any). */
  rows: readonly SessionMention[];
  /** The one row (index 0) that opens a deeper list, if this level has one. */
  category: SessionMenuLevel | null;
  /** The open list, or ``null`` at the top level; files show only at the top level. */
  level: SessionMenuLevel | null;
  /** Open the list ``category`` names. */
  openCategory: () => void;
  /** Go up one level. */
  goBack: () => void;
}

export interface MentionBrowser {
  mentionIndex: number;
  mentionOpen: boolean;
  sessionMenu: SessionMenuView;
  mentionedItems: MentionItem[];
  setMentionedItems: React.Dispatch<React.SetStateAction<MentionItem[]>>;
  /** Tag a file (isDir=false) or whole folder (isDir=true) as an inline token. */
  attachMention: (path: string, isDir: boolean) => void;
  /** Drill into a folder: rewrite the token to ``@<dir>/`` and keep browsing. */
  openMentionDir: (path: string) => void;
  mentionedSessions: SessionMention[];
  setMentionedSessions: React.Dispatch<React.SetStateAction<SessionMention[]>>;
  /** Tag a session, replacing the typed ``@query`` with its token. */
  attachSession: (session: SessionMention) => void;
  /** Inserts pasted files as chips at the input's selection; returns those it could not insert. */
  attachPastedFiles: (
    files: readonly File[],
    target: ComposerTextTarget,
    trayFiles: readonly File[],
  ) => File[];
  /** The pasted-file chips in the draft, released ones excluded. */
  mentionedAttachments: PastedAttachment[];
  /** Tokens of everything tagged so far, for highlighting and Backspace. */
  mentionTokens: string[];
  /**
   * Tag files from outside the menu (the file viewer's "Attach to agent"), reusing each one's
   * token if it is already tagged or was released; returns the tokens, in order.
   */
  tagFiles: (files: readonly MentionItem[]) => string[];
  /** Every token a tag owns, active or released, for scanning text the way the hook does. */
  ownedMentionTokens: () => string[];
  /** The pasted-chip tokens, live and released. */
  ownedChipTokens: () => string[];
  /** Replace the draft's tags, forgetting released ones: a send, recall or new draft. */
  resetMentions: (mentions?: TaggedMentions) => void;
  /**
   * Feed every text change in, so an undo or redo that restores a token restores its tag. Call it
   * before the change reaches the draft, so it sees the token leave the input while its tag is active.
   */
  noteTextChange: (event: ComposerInputEvent) => void;
  /** Backspace at or inside a token deletes it whole; returns true when it did. */
  handleTokenBackspace: (e: ComposerKeyboardEvent) => boolean;
  /** Handle a key event for the open menu; returns true when it consumed it. */
  handleKeyDown: (e: ComposerKeyboardEvent) => boolean;
  /** Dismiss the menu (e.g. on blur). */
  dismiss: () => void;
}

/**
 * Shared ``@``-mention controller (sessions and files) for the in-session composer and the
 * new-session launcher. Owns the selection index, the tagged-chip list, and
 * the attach/drill/remove + keyboard behaviour; the composer owns the token
 * state and supplies the directory listing (its data source differs).
 */
export function useMentionBrowser(params: MentionBrowserParams): MentionBrowser {
  const {
    mention,
    setMention,
    mentionEntries,
    sessionEntries = NO_SESSION_MENTIONS,
    text,
    setText,
    textareaRef,
    isMobile = false,
    initialMentions,
    segments,
    tags,
    onDemoteChips,
  } = params;
  const [mentionIndex, setMentionIndex] = useState(-1);
  const [localItems, setLocalItems] = useState<MentionItem[]>(() => initialMentions?.files ?? []);
  const [localSessions, setLocalSessions] = useState<SessionMention[]>(
    () => initialMentions?.sessions ?? [],
  );
  // Pasted chips live only in this composer; a draft that leaves it carries their files as tray files.
  const [mentionedAttachments, setMentionedAttachments] = useState<PastedAttachment[]>([]);
  const mentionedItems = tags ? tags.mentions.files : localItems;
  const mentionedSessions = tags ? tags.mentions.sessions : localSessions;
  const setMentionedItems: React.Dispatch<React.SetStateAction<MentionItem[]>> = tags
    ? (update) =>
        tags.updateMentions((current) => ({
          ...current,
          files: resolveUpdate(update, current.files),
        }))
    : setLocalItems;
  const setMentionedSessions: React.Dispatch<React.SetStateAction<SessionMention[]>> = tags
    ? (update) =>
        tags.updateMentions((current) => ({
          ...current,
          sessions: resolveUpdate(update, current.sessions),
        }))
    : setLocalSessions;
  // An opened list stays open for the "@" token it was opened from.
  const [browse, setBrowse] = useState<{ start: number; level: SessionMenuLevel } | null>(null);
  if (browse !== null && mention?.start !== browse.start) setBrowse(null);
  const level = browse !== null && mention?.start === browse.start ? browse.level : null;
  const subagentEntries = sessionEntries.filter((session) => session.isSubagent);
  // Top level: a bare "@" collapses sessions into one row; typing lists the top
  // matches inline. Sessions: a Subagents row, then the other sessions.
  let category: SessionMenuLevel | null = null;
  let sessionRows: readonly SessionMention[];
  if (level === "subagents") {
    sessionRows = subagentEntries;
  } else if (level === "sessions") {
    if (subagentEntries.length > 0) category = "subagents";
    sessionRows = sessionEntries.filter((session) => !session.isSubagent);
  } else if (mention?.query === "" && sessionEntries.length > 0) {
    category = "sessions";
    sessionRows = NO_SESSION_MENTIONS;
  } else {
    sessionRows = sessionEntries.slice(0, SESSION_MENTION_CAP);
  }
  const fileRows = level === null ? mentionEntries : NO_FILE_ENTRIES;
  // Rows: the category row (if shown), then sessions, then files.
  const sessionOffset = category ? 1 : 0;
  const fileOffset = sessionOffset + sessionRows.length;
  const rowCount = fileOffset + fileRows.length;
  const mentionOpen = rowCount > 0 || level !== null;

  // Pre-select the top row whenever the listing changes — lets Enter/Tab act on
  // the top hit without arrowing first. Keyed by type+path so a file and a dir
  // of the same name stay distinct. (Render-phase state adjustment, the React
  // "store-previous-props" pattern — mirrors the slash menu's reset.)
  const prevMentionMatchesRef = useRef<string[]>([]);
  const mentionEntryKeys = [
    ...(category ? [`category:${category}`] : []),
    ...sessionRows.map((session) => `session:${session.id}`),
    ...fileRows.map((e) => `${e.type}:${e.path}`),
  ];
  if (
    mentionEntryKeys.length !== prevMentionMatchesRef.current.length ||
    mentionEntryKeys.some((k, i) => k !== prevMentionMatchesRef.current[i])
  ) {
    // Keep the highlighted row when late results (debounced search) shift it,
    // so Enter never lands on a row inserted above the one the user saw.
    const highlighted = prevMentionMatchesRef.current[mentionIndex];
    const kept = highlighted === undefined ? -1 : mentionEntryKeys.indexOf(highlighted);
    prevMentionMatchesRef.current = mentionEntryKeys;
    setMentionIndex(kept >= 0 ? kept : mentionEntryKeys.length > 0 ? 0 : -1);
  }

  const mentionTokens = tagTokensOf({
    files: mentionedItems,
    sessions: mentionedSessions,
    attachments: mentionedAttachments,
  });

  // Tags whose tokens left the draft. They keep their tokens, so no other target is given
  // one while an undo could still bring it back; a reset forgets them.
  const releasedRef = useRef<LiveMentions>(NO_TAGS);
  // Restore points by token: where a token was last removed, for an undo to bring it back.
  const restorePointsRef = useRef(new Map<string, RestorePoint>());
  // The latest tags, for callers that run outside a render (the file viewer's drain).
  const activeRef = useRef<LiveMentions>({
    files: mentionedItems,
    sessions: mentionedSessions,
    attachments: mentionedAttachments,
  });
  useLayoutEffect(() => {
    activeRef.current = {
      files: mentionedItems,
      sessions: mentionedSessions,
      attachments: mentionedAttachments,
    };
  });

  // The pasted-chip tokens, live and released: the names a picked file must not take.
  const ownedChipTokens = () =>
    [...attachmentsOf(activeRef.current), ...attachmentsOf(releasedRef.current)].map(
      (attachment) => attachment.token,
    );

  // Every token a tag owns, active or released; new tags take none of them.
  const ownedMentionTokens = () => [
    ...tagTokensOf(activeRef.current),
    ...tagTokensOf(releasedRef.current),
  ];

  // Release tags whose last token was deleted (Backspace, cut, select-and-type).
  // Scanning with every token at once keeps a token that is only the start of a
  // longer one from counting as present. Caller-owned tags are filtered by the caller.
  const draftSegments = segments ?? [text];
  const segmentsKey = draftSegments.join("\u0000");
  useEffect(() => {
    const owned = ownedMentionTokens();
    if (owned.length === 0) return;
    const present = tokensIn(draftSegments, owned);
    // A caller that owns the file and session tags releases its own; chips are local in both modes.
    const goneFiles = tags ? [] : mentionedItems.filter((it) => !present.has(fileMentionToken(it)));
    const goneSessions = tags
      ? []
      : mentionedSessions.filter((it) => !present.has(sessionTagToken(it)));
    const goneAttachments = mentionedAttachments.filter((it) => !present.has(it.token));
    if (goneFiles.length === 0 && goneSessions.length === 0 && goneAttachments.length === 0) return;
    const gone = { files: goneFiles, sessions: goneSessions, attachments: goneAttachments };
    releasedRef.current = mergeTags(releasedRef.current, gone);
    if (goneFiles.length > 0) {
      setMentionedItems((prev) => prev.filter((it) => present.has(fileMentionToken(it))));
    }
    if (goneSessions.length > 0) {
      setMentionedSessions((prev) => prev.filter((it) => present.has(sessionTagToken(it))));
    }
    if (goneAttachments.length > 0) {
      setMentionedAttachments((prev) => prev.filter((it) => present.has(it.token)));
    }
    // draftSegments is captured through segmentsKey, which changes with its content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [segmentsKey, mentionedItems, mentionedSessions, mentionedAttachments]);

  // Take released tags back as active ones, and drop the restore points they used.
  const reclaim = (restored: LiveMentions) => {
    const restoredAttachments = attachmentsOf(restored);
    if (
      restored.files.length === 0 &&
      restored.sessions.length === 0 &&
      restoredAttachments.length === 0
    ) {
      return;
    }
    releasedRef.current = withoutTags(releasedRef.current, restored);
    for (const token of tagTokensOf(restored)) restorePointsRef.current.delete(token);
    if (restored.files.length > 0) setMentionedItems((prev) => [...prev, ...restored.files]);
    if (restored.sessions.length > 0) {
      setMentionedSessions((prev) => [...prev, ...restored.sessions]);
    }
    if (restoredAttachments.length > 0) {
      setMentionedAttachments((prev) => [...prev, ...restoredAttachments]);
    }
  };

  // Each input's text as its last change left it, the baseline for that input's next change. A
  // controlled replacement clears that input's undo history, so an undo only replays changes that
  // reported themselves here.
  const lastSeenRef = useRef(new WeakMap<object, string>());
  // The active input's live value is its baseline, so its first change after a controlled update
  // still knows the text it replaced.
  useLayoutEffect(() => {
    const input = textareaRef.current;
    if (input?.isConnected) lastSeenRef.current.set(input, input.value);
  });

  const noteTextChange = (event: ComposerInputEvent) => {
    const input = event.currentTarget;
    const after = input.value;
    const before = lastSeenRef.current.get(input) ?? "";
    lastSeenRef.current.set(input, after);
    const owned = ownedMentionTokens();
    const restorePoints = restorePointsRef.current;
    const direction = historyDirectionOf(event);
    if (direction !== null) {
      // Reclaim only in the input the token left, at its pre-removal text, in the reversing
      // direction. Walking back, that text is first reached by the reversing undo: an ordinary
      // change landing on it earlier would have dropped the point. Redo mirrors this.
      const released = releasedRef.current;
      const restores = (token: string) => {
        const point = restorePoints.get(token);
        return point?.input === input && point.reclaimedBy === direction && point.value === after;
      };
      reclaim({
        files: released.files.filter((it) => restores(fileMentionToken(it))),
        sessions: released.sessions.filter((it) => restores(sessionTagToken(it))),
        attachments: attachmentsOf(released).filter((it) => restores(it.token)),
      });
    } else {
      // Text typed or pasted back to a restore point's value is literal now, so that point is stale.
      // An undo chain that later reaches the original deletion restores plain text, not the tag.
      for (const [token, point] of restorePoints) {
        if (point.input === input && point.value === after) restorePoints.delete(token);
      }
    }
    // A change that removes an active tag's token records where it was removed from. An undo's
    // removal leaves its text ahead in history, so a redo reclaims it; any other removal leaves it
    // behind, so an undo does.
    const afterTokens = tokensIn([after], owned);
    const active = new Set(tagTokensOf(activeRef.current));
    for (const token of tokensIn([before], owned)) {
      if (afterTokens.has(token)) continue;
      if (active.has(token)) {
        const reclaimedBy = direction === "undo" ? "redo" : "undo";
        restorePoints.set(token, { input, value: before, reclaimedBy });
      }
    }
  };

  const resetMentions = (mentions?: TaggedMentions) => {
    // Every reset goes through here: a live chip leaves as a tray file, so its File is kept. The
    // latest chips come from activeRef, which a paste updates before the next render.
    const liveChips = attachmentsOf(activeRef.current);
    if (liveChips.length > 0) {
      onDemoteChips?.(liveChips.map((attachment) => attachment.file));
      activeRef.current = { ...activeRef.current, attachments: [] };
      setMentionedAttachments([]);
    }
    releasedRef.current = NO_TAGS;
    restorePointsRef.current.clear();
    setMentionedItems(mentions?.files ?? []);
    setMentionedSessions(mentions?.sessions ?? []);
  };

  const placeCaret = (caret: number) =>
    queueMicrotask(() => {
      const ta = textareaRef.current;
      if (ta) ta.setSelectionRange(caret, caret);
      ta?.focus();
    });

  // The input's own text: keys typed in quick succession can run before `text` re-renders.
  const liveText = (target: ComposerTextTarget | null) =>
    target?.isConnected ? target.value : text;

  // Replace [start, end) of the draft as its own undo step, with the caret placed in the
  // same edit, so a key typed right after it lands at `caret`.
  const editDraft = (
    target: ComposerTextTarget | null,
    start: number,
    end: number,
    insert: string,
    caret: number,
  ): boolean => {
    if (target?.isConnected) {
      // The before-text is the live value, even if a controlled update skipped its notice.
      lastSeenRef.current.set(target, target.value);
      // A locked input refuses the edit; callers then leave the tags as they were.
      if (!insertComposerText(target, insert, start, end, { separateUndo: true })) return false;
      if (target.selectionStart !== caret || target.selectionEnd !== caret) {
        target.setSelectionRange(caret, caret);
      }
      target.focus();
      return true;
    }
    setText(text.slice(0, start) + insert + text.slice(end));
    placeCaret(caret);
    return true;
  };

  // Replace [start, end) with the token, spaced off from the words around it, caret after it.
  // With ``skipExistingSpace`` the caret also passes a space already after the token, as a mention
  // pick does; otherwise it stops after what was inserted.
  const insertTokenAt = (
    target: ComposerTextTarget | null,
    start: number,
    end: number,
    token: string,
    skipExistingSpace = false,
  ): boolean => {
    const current = liveText(target);
    const lead = start > 0 && !/\s/.test(current[start - 1]!) ? " " : "";
    const trail = /^\s/.test(current.slice(end)) ? "" : " ";
    const tokenStart = start + lead.length;
    const caret = tokenStart + token.length + (skipExistingSpace ? 1 : trail.length);
    return editDraft(target, start, end, lead + token + trail, caret);
  };

  // Swap the typed "@query" for the token plus one space, caret after it.
  const insertToken = (active: MentionState, token: string): boolean => {
    if (!insertTokenAt(textareaRef.current, active.start, active.end, token, true)) return false;
    setMention(null);
    setMentionIndex(-1);
    return true;
  };

  const attachPastedFiles = (
    files: readonly File[],
    target: ComposerTextTarget,
    trayFiles: readonly File[],
  ): File[] => {
    // The input may have unmounted between the paste and this call.
    if (!target.isConnected) return [...files];
    const added: PastedAttachment[] = [];
    const addedNames: string[] = [];
    for (const [index, file] of files.entries()) {
      // A chip is the composer's own reference, so it also avoids the spellings already typed.
      const named = nameAttachmentFile(file, {
        tokens: [...ownedMentionTokens(), ...added.map((it) => it.token)],
        trayNames: [...trayFiles.map(attachmentFilename), ...addedNames],
        text: [liveText(target), ...draftSegments],
      });
      const token = `@${named.name}`;
      if (!insertTokenAt(target, target.selectionStart, target.selectionEnd, token)) {
        return files.slice(index);
      }
      added.push({ token, file: named });
      // Record the chip now: a file refused later in this paste must see it when it is named.
      activeRef.current = {
        ...activeRef.current,
        attachments: [...attachmentsOf(activeRef.current), { token, file: named }],
      };
      addedNames.push(named.name);
    }
    if (added.length > 0) {
      setMention(null);
      setMentionIndex(-1);
      setMentionedAttachments((prev) => [...prev, ...added]);
    }
    return [];
  };

  const attachSession = (session: SessionMention) => {
    if (!mention) return;
    const existing = mentionedSessions.find((it) => it.id === session.id);
    const released = releasedRef.current.sessions.find((it) => it.id === session.id);
    const token =
      existing?.token ?? released?.token ?? sessionMentionToken(session, ownedMentionTokens());
    if (!insertToken(mention, token) || existing) return;
    if (released) reclaim({ files: [], sessions: [{ ...released, token }] });
    else setMentionedSessions((prev) => [...prev, { ...session, token }]);
  };

  const handleTokenBackspace = (e: ComposerKeyboardEvent): boolean => {
    if (e.key !== "Backspace" || e.altKey || e.metaKey || e.ctrlKey) return false;
    const ta = e.currentTarget;
    if (ta.selectionStart !== ta.selectionEnd) return false;
    const current = liveText(ta);
    const range = mentionTokenAtCaret(current, ta.selectionStart, mentionTokens);
    if (!range) return false;
    if (!editDraft(ta, range.start, range.end, "", range.start)) return false;
    e.preventDefault();
    // The deleted token may have been the active "@" query; recompute it so a
    // following Enter can't complete a mention that no longer exists.
    setMention(
      detectMentionAt(current.slice(0, range.start) + current.slice(range.end), range.start),
    );
    setMentionIndex(-1);
    return true;
  };

  // The tag for a file, reusing an active or released one with the same attachment key (path,
  // dir-ness and range), the identity the store queue uses, so the "@" menu and the file
  // viewer's "Attach to agent" never disagree about duplicates.
  const claimFile = (
    file: MentionItem,
    taken: string[],
  ): { item: MentionItem; source: "active" | "released" | "new" } => {
    const itemKey = composerAttachmentKey(file);
    const active = activeRef.current.files.find((it) => composerAttachmentKey(it) === itemKey);
    if (active) return { item: active, source: "active" };
    const released = releasedRef.current.files.find((it) => composerAttachmentKey(it) === itemKey);
    if (released) return { item: released, source: "released" };
    return { item: { ...file, token: newFileMentionToken(file, taken) }, source: "new" };
  };

  const attachMention = (path: string, isDir: boolean) => {
    if (!mention) return;
    const { item, source } = claimFile({ path, isDir }, ownedMentionTokens());
    if (!insertToken(mention, fileMentionToken(item))) return;
    if (source === "released") reclaim({ files: [item], sessions: [] });
    if (source === "new") setMentionedItems((prev) => [...prev, item]);
  };

  const tagFiles = (files: readonly MentionItem[]): string[] => {
    const taken = ownedMentionTokens();
    const fresh: MentionItem[] = [];
    const reclaimed: MentionItem[] = [];
    const tokens = files.map((file) => {
      const pending = [...fresh, ...reclaimed].find(
        (it) => composerAttachmentKey(it) === composerAttachmentKey(file),
      );
      if (pending) return fileMentionToken(pending);
      const { item, source } = claimFile(file, taken);
      if (source === "new") {
        fresh.push(item);
        taken.push(fileMentionToken(item));
      }
      if (source === "released") reclaimed.push(item);
      return fileMentionToken(item);
    });
    reclaim({ files: reclaimed, sessions: [] });
    if (fresh.length > 0) setMentionedItems((prev) => [...prev, ...fresh]);
    return tokens;
  };

  const openMentionDir = (path: string) => {
    if (!mention) return;
    const inserted = `@${path}/`;
    const caret = mention.start + inserted.length;
    if (!editDraft(textareaRef.current, mention.start, mention.end, inserted, caret)) return;
    setMention({ query: `${path}/`, start: mention.start, end: caret });
    setMentionIndex(0);
  };

  const dismiss = () => {
    if (!mention) return;
    setMention(null);
    setMentionIndex(-1);
  };

  const openCategory = () => {
    if (!mention || !category) return;
    setBrowse({ start: mention.start, level: category });
    setMentionIndex(0);
    textareaRef.current?.focus();
  };

  const goBack = () => {
    setBrowse(
      level === "subagents" && mention ? { start: mention.start, level: "sessions" } : null,
    );
    setMentionIndex(0);
    textareaRef.current?.focus();
  };

  const handleKeyDown = (e: ComposerKeyboardEvent): boolean => {
    if (!mentionOpen) return false;
    if (e.key === "Escape" && level !== null) {
      e.preventDefault();
      goBack();
      return true;
    }
    if (rowCount === 0) return false;
    const onCategory = category !== null && mentionIndex === 0;
    const activeSession =
      mentionIndex >= sessionOffset && mentionIndex < fileOffset
        ? sessionRows[mentionIndex - sessionOffset]
        : undefined;
    const active = mentionIndex >= fileOffset ? fileRows[mentionIndex - fileOffset] : undefined;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setMentionIndex((i) => (i + 1) % rowCount);
      return true;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setMentionIndex((i) => (i <= 0 ? rowCount - 1 : i - 1));
      return true;
    }
    if (
      onCategory &&
      (e.key === "Tab" || e.key === "ArrowRight" || (e.key === "Enter" && !e.shiftKey && !isMobile))
    ) {
      e.preventDefault();
      openCategory();
      return true;
    }
    if (activeSession && (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && !isMobile))) {
      e.preventDefault();
      attachSession(activeSession);
      return true;
    }
    // Enter: open a folder (drill in) or attach a file. Tab: attach the
    // highlighted row as a unit — whole folder or file — without drilling.
    if (e.key === "Enter" && !e.shiftKey && !isMobile && active) {
      e.preventDefault();
      if (active.type === "directory") openMentionDir(active.path);
      else attachMention(active.path, false);
      return true;
    }
    if (e.key === "Tab" && active) {
      e.preventDefault();
      attachMention(active.path, active.type === "directory");
      return true;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      dismiss();
      return true;
    }
    return false;
  };

  return {
    mentionIndex,
    mentionOpen,
    sessionMenu: { rows: sessionRows, category, level, openCategory, goBack },
    mentionedItems,
    setMentionedItems,
    attachMention,
    openMentionDir,
    mentionedSessions,
    setMentionedSessions,
    attachSession,
    attachPastedFiles,
    mentionedAttachments,
    mentionTokens,
    tagFiles,
    ownedMentionTokens,
    ownedChipTokens,
    resetMentions,
    noteTextChange,
    handleTokenBackspace,
    handleKeyDown,
    dismiss,
  };
}
