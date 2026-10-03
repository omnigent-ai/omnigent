import { type KeyboardEvent, type RefObject, useEffect, useState } from "react";

import { parseMentionToken, type MentionItem, type MentionState } from "@/lib/composerMentions";
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
  /** Attach a file (isDir=false) or whole folder (isDir=true) as a chip. */
  attachMention: (path: string, isDir: boolean) => void;
  /** Drill into a folder: rewrite the token to ``@<dir>/`` and keep browsing. */
  openMentionDir: (path: string) => void;
  removeMentionedItem: (index: number) => void;
  /** Handle a key event for the open menu; returns true when it consumed it. */
  handleKeyDown: (e: KeyboardEvent<HTMLTextAreaElement>) => boolean;
  /** Dismiss the menu (e.g. on blur). */
  dismiss: () => void;
}

/**
 * Shared ``@``-file-mention controller for the in-session composer and the
 * new-session launcher. Owns the selection index, the tagged-chip list, and
 * the attach/drill/remove + keyboard behaviour; the composer owns the token
 * state and supplies the directory listing (its data source differs).
 */
export function useMentionBrowser(params: MentionBrowserParams): MentionBrowser {
  const {
    mention,
    setMention,
    mentionEntries,
    text,
    setText,
    textareaRef,
    isMobile = false,
  } = params;
  const [mentionIndex, setMentionIndex] = useState(-1);
  const [mentionedItems, setMentionedItems] = useState<MentionItem[]>([]);
  const mentionOpen = mentionEntries.length > 0;

  // Pre-select the top row whenever the listing changes — lets Enter attach or
  // ArrowRight open the top hit without arrowing first. The serialized key also
  // distinguishes a file and directory that happen to share a path.
  const mentionEntryKey = mentionEntries.map((entry) => `${entry.type}:${entry.path}`).join("\0");
  useEffect(() => {
    setMentionIndex(mentionEntries.length > 0 ? 0 : -1);
  }, [mentionEntryKey, mentionEntries.length]);

  const attachMention = (path: string, isDir: boolean) => {
    if (!mention) return;
    setText(text.slice(0, mention.start) + text.slice(mention.end));
    // Dedup on the shared attachment key (path + dir-ness + range) — the same
    // identity the store queue uses — so the "@" menu and the file viewer's
    // "Attach to agent" never disagree about what counts as a duplicate.
    const item: MentionItem = { path, isDir };
    const itemKey = composerAttachmentKey(item);
    setMentionedItems((prev) =>
      prev.some((it) => composerAttachmentKey(it) === itemKey) ? prev : [...prev, item],
    );
    setMention(null);
    setMentionIndex(-1);
    // Restore the caret to where the token was so typing continues naturally.
    queueMicrotask(() => {
      const ta = textareaRef.current;
      if (ta) ta.setSelectionRange(mention.start, mention.start);
      ta?.focus();
    });
  };

  const replaceMentionQuery = (query: string) => {
    if (!mention) return;
    const inserted = query ? `@${query}/` : "@";
    const next = text.slice(0, mention.start) + inserted + text.slice(mention.end);
    setText(next);
    const caret = mention.start + inserted.length;
    setMention({ query: query ? `${query}/` : "", start: mention.start, end: caret });
    setMentionIndex(0);
    queueMicrotask(() => {
      const ta = textareaRef.current;
      if (ta) ta.setSelectionRange(caret, caret);
      ta?.focus();
    });
  };

  const openMentionDir = (path: string) => replaceMentionQuery(path);

  const openMentionParent = () => {
    if (!mention) return false;
    const { dir } = parseMentionToken(mention.query);
    if (!dir) return false;
    const slash = dir.lastIndexOf("/");
    replaceMentionQuery(slash >= 0 ? dir.slice(0, slash) : "");
    return true;
  };

  const removeMentionedItem = (index: number) =>
    setMentionedItems((prev) => prev.filter((_, i) => i !== index));

  const dismiss = () => {
    if (!mention) return;
    setMention(null);
    setMentionIndex(-1);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    const plainKey = !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey;
    if (e.key === "ArrowLeft" && plainKey && openMentionParent()) {
      e.preventDefault();
      return true;
    }
    if (!mentionOpen) return false;
    const active = mentionIndex >= 0 ? mentionEntries[mentionIndex] : undefined;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setMentionIndex((i) => (i + 1) % mentionEntries.length);
      return true;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setMentionIndex((i) => (i <= 0 ? mentionEntries.length - 1 : i - 1));
      return true;
    }
    // Enter attaches the highlighted file or whole folder. ArrowRight opens a
    // folder; ArrowLeft returns to its parent. Tab and Backspace keep their
    // native behavior.
    if (e.key === "Enter" && !e.shiftKey && !isMobile && active) {
      e.preventDefault();
      attachMention(active.path, active.type === "directory");
      return true;
    }
    if (e.key === "ArrowRight" && plainKey && active?.type === "directory") {
      e.preventDefault();
      openMentionDir(active.path);
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
    removeMentionedItem,
    handleKeyDown,
    dismiss,
  };
}
