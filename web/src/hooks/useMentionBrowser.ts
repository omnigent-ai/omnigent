import { type RefObject, useRef, useState } from "react";

import type { MentionItem, MentionState } from "@/lib/composerMentions";
import { composerAttachmentKey } from "@/store/chatStore";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

/**
 * Inputs the host composer supplies. The data source (workspace API vs. host
 * filesystem) and the mention-token state live in the composer — only the
 * stateful glue (selection index, tagged chips, semantic selection handlers,
 * and top-row preselect) is shared here, so the two composers
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
  selectPrevious: () => boolean;
  selectNext: () => boolean;
  /** Enter drills into folders; Tab attaches folders whole. */
  accept: (behavior: "openOrAttach" | "attach") => boolean;
  /** Dismiss the menu (e.g. on blur); reports whether it was open. */
  dismiss: () => boolean;
}

/**
 * Shared ``@``-file-mention controller for the in-session composer and the
 * new-session launcher. Owns the selection index, the tagged-chip list, and
 * the attach/drill/remove + selection behaviour; the composer owns the token
 * state and supplies the directory listing (its data source differs).
 */
export function useMentionBrowser(params: MentionBrowserParams): MentionBrowser {
  const { mention, setMention, mentionEntries, text, setText, textareaRef } = params;
  const [mentionIndex, setMentionIndex] = useState(-1);
  const [mentionedItems, setMentionedItems] = useState<MentionItem[]>([]);
  const mentionOpen = mentionEntries.length > 0;

  // Pre-select the top row whenever the listing changes — lets Enter/Tab act on
  // the top hit without arrowing first. Keyed by type+path so a file and a dir
  // of the same name stay distinct. (Render-phase state adjustment, the React
  // "store-previous-props" pattern — mirrors the slash menu's reset.)
  const prevMentionMatchesRef = useRef<string[]>([]);
  const mentionEntryKeys = mentionEntries.map((e) => `${e.type}:${e.path}`);
  if (
    mentionEntryKeys.length !== prevMentionMatchesRef.current.length ||
    mentionEntryKeys.some((k, i) => k !== prevMentionMatchesRef.current[i])
  ) {
    prevMentionMatchesRef.current = mentionEntryKeys;
    setMentionIndex(mentionEntryKeys.length > 0 ? 0 : -1);
  }

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

  const removeMentionedItem = (index: number) =>
    setMentionedItems((prev) => prev.filter((_, i) => i !== index));

  const dismiss = (): boolean => {
    if (!mention) return false;
    setMention(null);
    setMentionIndex(-1);
    return true;
  };

  const selectPrevious = (): boolean => {
    if (!mentionOpen) return false;
    setMentionIndex((index) => (index <= 0 ? mentionEntries.length - 1 : index - 1));
    return true;
  };

  const selectNext = (): boolean => {
    if (!mentionOpen) return false;
    setMentionIndex((index) => (index + 1) % mentionEntries.length);
    return true;
  };

  const accept = (behavior: "openOrAttach" | "attach"): boolean => {
    if (!mentionOpen) return false;
    const active = mentionIndex >= 0 ? mentionEntries[mentionIndex] : undefined;
    if (!active) return false;
    if (behavior === "openOrAttach" && active.type === "directory") {
      openMentionDir(active.path);
    } else {
      attachMention(active.path, behavior === "attach" && active.type === "directory");
    }
    return true;
  };

  return {
    mentionIndex,
    mentionOpen,
    mentionedItems,
    setMentionedItems,
    attachMention,
    openMentionDir,
    removeMentionedItem,
    selectPrevious,
    selectNext,
    accept,
    dismiss,
  };
}
