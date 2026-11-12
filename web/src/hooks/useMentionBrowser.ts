import { type KeyboardEvent, type RefObject, useEffect, useRef, useState } from "react";

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
  type SessionMention,
  sessionMentionToken,
  type TaggedMentions,
} from "@/lib/composerMentions";
import { composerAttachmentKey } from "@/store/chatStore";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

const NO_FILE_ENTRIES: WorkspaceFile[] = [];

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
  textareaRef: RefObject<HTMLTextAreaElement | null>;
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
  /** Tokens of everything tagged so far, for highlighting and Backspace. */
  mentionTokens: string[];
  /** Backspace at or inside a token deletes it whole; returns true when it did. */
  handleTokenBackspace: (e: KeyboardEvent<HTMLTextAreaElement>) => boolean;
  /** Handle a key event for the open menu; returns true when it consumed it. */
  handleKeyDown: (e: KeyboardEvent<HTMLTextAreaElement>) => boolean;
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
  } = params;
  const [mentionIndex, setMentionIndex] = useState(-1);
  const [mentionedItems, setMentionedItems] = useState<MentionItem[]>(
    () => initialMentions?.files ?? [],
  );
  const [mentionedSessions, setMentionedSessions] = useState<SessionMention[]>(
    () => initialMentions?.sessions ?? [],
  );
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

  const mentionTokens = [
    ...mentionedItems.map(fileMentionToken),
    ...mentionedSessions.map((it) => it.token ?? `@${it.title}`),
  ];

  // Release tags whose last token was deleted (Backspace, cut, select-and-type).
  // Scanning with every token at once keeps a token that is only the start of a
  // longer one from counting as present.
  const draftSegments = segments ?? [text];
  const segmentsKey = draftSegments.join("\u0000");
  useEffect(() => {
    const tokens = [
      ...mentionedItems.map(fileMentionToken),
      ...mentionedSessions.map((it) => it.token ?? `@${it.title}`),
    ];
    if (tokens.length === 0) return;
    const present = new Set(
      draftSegments.flatMap((segment) => findMentionTokens(segment, tokens).map((r) => r.token)),
    );
    if (!mentionedItems.every((it) => present.has(fileMentionToken(it)))) {
      setMentionedItems((prev) => prev.filter((it) => present.has(fileMentionToken(it))));
    }
    if (!mentionedSessions.every((it) => present.has(it.token ?? `@${it.title}`))) {
      setMentionedSessions((prev) => prev.filter((it) => present.has(it.token ?? `@${it.title}`)));
    }
    // draftSegments is captured through segmentsKey, which changes with its content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [segmentsKey, mentionedItems, mentionedSessions]);

  const placeCaret = (caret: number) =>
    queueMicrotask(() => {
      const ta = textareaRef.current;
      if (ta) ta.setSelectionRange(caret, caret);
      ta?.focus();
    });

  // Swap the typed "@query" for the token plus one space, caret after it.
  const insertToken = (active: MentionState, token: string) => {
    const after = text.slice(active.end);
    const gap = /^\s/.test(after) ? "" : " ";
    setText(text.slice(0, active.start) + token + gap + after);
    setMention(null);
    setMentionIndex(-1);
    placeCaret(active.start + token.length + 1);
  };

  const attachSession = (session: SessionMention) => {
    if (!mention) return;
    const existing = mentionedSessions.find((it) => it.id === session.id);
    const token = existing?.token ?? sessionMentionToken(session, mentionTokens);
    if (!existing) setMentionedSessions((prev) => [...prev, { ...session, token }]);
    insertToken(mention, token);
  };

  const handleTokenBackspace = (e: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (e.key !== "Backspace" || e.altKey || e.metaKey || e.ctrlKey) return false;
    const ta = e.currentTarget;
    if (ta.selectionStart !== ta.selectionEnd) return false;
    const range = mentionTokenAtCaret(text, ta.selectionStart, mentionTokens);
    if (!range) return false;
    e.preventDefault();
    const next = text.slice(0, range.start) + text.slice(range.end);
    setText(next);
    // The deleted token may have been the active "@" query; recompute it so a
    // following Enter can't complete a mention that no longer exists.
    setMention(detectMentionAt(next, range.start));
    setMentionIndex(-1);
    placeCaret(range.start);
    return true;
  };

  const attachMention = (path: string, isDir: boolean) => {
    if (!mention) return;
    // Dedup on the shared attachment key (path + dir-ness + range) — the same
    // identity the store queue uses — so the "@" menu and the file viewer's
    // "Attach to agent" never disagree about what counts as a duplicate.
    const itemKey = composerAttachmentKey({ path, isDir });
    const existing = mentionedItems.find((it) => composerAttachmentKey(it) === itemKey);
    const item: MentionItem = existing ?? {
      path,
      isDir,
      token: newFileMentionToken({ path, isDir }, mentionTokens),
    };
    if (!existing) setMentionedItems((prev) => [...prev, item]);
    insertToken(mention, fileMentionToken(item));
  };

  const openMentionDir = (path: string) => {
    if (!mention) return;
    const inserted = `@${path}/`;
    const next = text.slice(0, mention.start) + inserted + text.slice(mention.end);
    setText(next);
    const caret = mention.start + inserted.length;
    setMention({ query: `${path}/`, start: mention.start, end: caret });
    setMentionIndex(0);
    queueMicrotask(() => {
      const ta = textareaRef.current;
      if (ta) ta.setSelectionRange(caret, caret);
      ta?.focus();
    });
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

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): boolean => {
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
    mentionTokens,
    handleTokenBackspace,
    handleKeyDown,
    dismiss,
  };
}
