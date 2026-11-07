import { type KeyboardEvent, type RefObject, useRef, useState } from "react";

import {
  fileMentionToken,
  mentionTokenAtCaret,
  NO_SESSION_MENTIONS,
  type MentionItem,
  type MentionState,
  type SessionMention,
  sessionMentionToken,
} from "@/lib/composerMentions";
import { composerAttachmentKey } from "@/store/chatStore";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

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
  /** Matching sessions, listed above the files. Already filtered and capped. */
  sessionEntries?: readonly SessionMention[];
  /** The textarea value and a setter (which may also flag the draft dirty). */
  text: string;
  setText: (next: string) => void;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  /** On mobile, Enter inserts a newline rather than acting on the menu. */
  isMobile?: boolean;
}

export interface MentionBrowser {
  mentionIndex: number;
  mentionOpen: boolean;
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
  } = params;
  const [mentionIndex, setMentionIndex] = useState(-1);
  const [mentionedItems, setMentionedItems] = useState<MentionItem[]>([]);
  const [mentionedSessions, setMentionedSessions] = useState<SessionMention[]>([]);
  // Sessions occupy rows [0, sessionEntries.length); files follow.
  const rowCount = sessionEntries.length + mentionEntries.length;
  const mentionOpen = rowCount > 0;

  // Pre-select the top row whenever the listing changes — lets Enter/Tab act on
  // the top hit without arrowing first. Keyed by type+path so a file and a dir
  // of the same name stay distinct. (Render-phase state adjustment, the React
  // "store-previous-props" pattern — mirrors the slash menu's reset.)
  const prevMentionMatchesRef = useRef<string[]>([]);
  const mentionEntryKeys = [
    ...sessionEntries.map((session) => `session:${session.id}`),
    ...mentionEntries.map((e) => `${e.type}:${e.path}`),
  ];
  if (
    mentionEntryKeys.length !== prevMentionMatchesRef.current.length ||
    mentionEntryKeys.some((k, i) => k !== prevMentionMatchesRef.current[i])
  ) {
    prevMentionMatchesRef.current = mentionEntryKeys;
    setMentionIndex(mentionEntryKeys.length > 0 ? 0 : -1);
  }

  const mentionTokens = [
    ...mentionedItems.map(fileMentionToken),
    ...mentionedSessions.map((it) => it.token ?? `@${it.title}`),
  ];

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
    setText(text.slice(0, range.start) + text.slice(range.end));
    placeCaret(range.start);
    return true;
  };

  const attachMention = (path: string, isDir: boolean) => {
    if (!mention) return;
    // Dedup on the shared attachment key (path + dir-ness + range) — the same
    // identity the store queue uses — so the "@" menu and the file viewer's
    // "Attach to agent" never disagree about what counts as a duplicate.
    const item: MentionItem = { path, isDir };
    const itemKey = composerAttachmentKey(item);
    setMentionedItems((prev) =>
      prev.some((it) => composerAttachmentKey(it) === itemKey) ? prev : [...prev, item],
    );
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

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!mentionOpen) return false;
    const activeSession = mentionIndex >= 0 ? sessionEntries[mentionIndex] : undefined;
    const active =
      mentionIndex >= sessionEntries.length
        ? mentionEntries[mentionIndex - sessionEntries.length]
        : undefined;
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
