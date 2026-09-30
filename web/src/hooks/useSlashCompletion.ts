import { useLayoutEffect, useRef, useState, type RefObject } from "react";
import { rankedSlashCommandNames } from "@/components/SlashCommandMenu";

/** The slice of a textarea keydown the menu reads. */
export interface SlashCompletionKeyEvent {
  key: string;
  shiftKey: boolean;
  preventDefault: () => void;
}

/** What the focused composer surface reports about the send chord. */
export interface SlashCompletionKeyIntent {
  /**
   * True when the keystroke doubles as the send chord (Mod+Enter in
   * Mod+Enter send mode): completion and the loading swallow yield so the
   * message sends instead. Arrows and Escape are unaffected.
   */
  shouldPreferSendOverCompletion: boolean;
}

export interface UseSlashCompletionOptions {
  /** Raw composer draft. */
  text: string;
  /** Command inventory: prefixed name ("/model", "$review") to description. */
  commands: Record<string, string>;
  /** Explicit skill inventory, including names that collide with built-ins. */
  skills: Record<string, string>;
  textareaRef?: RefObject<HTMLTextAreaElement | null>;
  /**
   * The command prefix this surface's inventory uses ("$" for codex-native,
   * "/" otherwise). "/" always opens the menu as well.
   */
  prefix: "/" | "$";
  /** Skill discovery status, or null when discovery is not in play. */
  status: string | null;
  mobile: boolean;
  /**
   * Whether Enter completes the highlighted match on mobile. Surfaces where
   * Enter means newline keep this false; the loading swallow never completes
   * on mobile either way.
   */
  mobileEnterCompletes: boolean;
  /**
   * Inline Escape dismisses without changing text. For a lone command token,
   * when true, Escape clears the draft only if the menu has content (matches,
   * or discovery still in flight), so an idle Escape can fall through to
   * other handlers. When false, Escape always clears while the menu is open.
   */
  escapeClearsOnlyWithContent: boolean;
  /** Surface-level gate ANDed with the token shape (focus, attachments). */
  allowOpen: boolean;
  /** Called with the ranked, prefixed name chosen via Tab/Enter. */
  onSelect: (cmd: string) => void;
  /** Tab only fills the draft when selecting a command would execute it. */
  onTabComplete?: (cmd: string) => void;
  /** Clears the composer draft (Escape semantics). */
  clearText: () => void;
}

export interface UseSlashCompletionResult {
  /** Inventory for this token; inline suggestions contain only skills. */
  commands: Record<string, string>;
  builtinNames: ReadonlySet<string>;
  inline: boolean;
  onSelectionChange: (element: HTMLTextAreaElement) => void;
  complete: (cmd: string) => { text: string; caret: number };
  /** Whether the suggestions menu is open. */
  open: boolean;
  /** The text typed after the prefix while open, else "". */
  query: string;
  /** Ranked match names while open, else []. */
  matches: string[];
  /** Highlighted row index, -1 when nothing is highlighted. */
  index: number;
  /**
   * The draft is a lone command token, discovery is in flight, and
   * there is nothing to complete yet. Not gated on the menu being open —
   * submit blocking keys off it even while the composer is blurred.
   */
  pendingCompletion: boolean;
  /**
   * Handles one textarea keydown, returning true when the menu consumed it.
   * Consumption order: Escape, the loading swallow, arrow navigation, then
   * Tab/Enter completion; anything else falls through to the caller.
   */
  handleKey: (e: SlashCompletionKeyEvent, intent: SlashCompletionKeyIntent) => boolean;
}

export function useSlashCompletion({
  text,
  commands,
  skills,
  textareaRef,
  prefix,
  status,
  mobile,
  mobileEnterCompletes,
  escapeClearsOnlyWithContent,
  allowOpen,
  onSelect,
  onTabComplete = onSelect,
  clearText,
}: UseSlashCompletionOptions): UseSlashCompletionResult {
  const [selection, setSelection] = useState<{ text: string; start: number; end: number } | null>(
    null,
  );
  const [dismissed, setDismissed] = useState<{ text: string; start: number; end: number } | null>(
    null,
  );
  const pendingCaret = useRef<{ text: string; originalText: string; caret: number } | null>(null);
  const caret = selection?.text === text ? selection.start : text.length;
  const selectionEnd = selection?.text === text ? selection.end : caret;
  const before = text.slice(0, caret);
  const token = /\s$/.test(before) ? undefined : before.match(/(?:^|\s)([/$][\w:-]*)$/)?.[1];
  const hasPrefix = token?.startsWith("/") || token?.startsWith(prefix);
  const start = token ? caret - token.length : caret;
  const tokenSuffix = text.slice(caret).match(/^\S*/)?.[0] ?? "";
  const end = caret + tokenSuffix.length;
  const inline = text.slice(0, start).trim().length > 0 || text.slice(end).trim().length > 0;
  const builtinNames = new Set(
    Object.keys(commands).filter((name) => !Object.hasOwn(skills, name)),
  );
  const menuCommands = inline ? skills : commands;
  const baseOpen = Boolean(hasPrefix) && caret === selectionEnd && /^[\w:-]*$/.test(tokenSuffix);
  const isDismissed =
    dismissed?.text === text && dismissed.start === start && dismissed.end === end;
  const [previousText, setPreviousText] = useState(text);
  if (previousText !== text) {
    setPreviousText(text);
    if (dismissed?.text !== text) setDismissed(null);
  }
  const open = allowOpen && baseOpen && !isDismissed;
  // Ranked on the token shape alone: a pending completion still reports
  // while the menu is blurred closed, since submit gating keys off it.
  // The returned query/matches stay gated on open.
  const baseQuery = baseOpen ? (token?.slice(1) ?? "") : "";
  // Kept in sync with what the menu renders so keyboard nav indexes into
  // the same list.
  const baseMatches = baseOpen
    ? rankedSlashCommandNames(menuCommands, baseQuery, builtinNames)
    : [];
  const query = open ? baseQuery : "";
  const matches = open ? baseMatches : [];
  const pendingCompletion =
    baseOpen && !inline && !isDismissed && status === "loading" && baseMatches.length === 0;

  const [index, setIndex] = useState(-1);
  // New queries select the first match; asynchronous arrivals retain the
  // selected name. Track the previous render in state so discarded renders
  // cannot consume an update.
  const [previousMatches, setPreviousMatches] = useState<{
    query: string;
    names: string[];
  }>({ query: "", names: [] });
  if (
    query !== previousMatches.query ||
    matches.length !== previousMatches.names.length ||
    matches.some((m, i) => m !== previousMatches.names[i])
  ) {
    const previousName = previousMatches.names[index];
    const retainedIndex =
      previousMatches.query === query && previousName ? matches.indexOf(previousName) : -1;
    setPreviousMatches({ query, names: matches });
    setIndex(retainedIndex >= 0 ? retainedIndex : matches.length > 0 ? 0 : -1);
  }

  function handleKey(
    e: SlashCompletionKeyEvent,
    { shouldPreferSendOverCompletion }: SlashCompletionKeyIntent,
  ): boolean {
    if (
      open &&
      e.key === "Escape" &&
      (!escapeClearsOnlyWithContent || matches.length > 0 || status != null)
    ) {
      e.preventDefault();
      if (inline) setDismissed({ text, start, end });
      else clearText();
      setIndex(-1);
      return true;
    }
    // A loading-only menu has no completion yet; don't submit the partial token.
    if (
      open &&
      status === "loading" &&
      matches.length === 0 &&
      !shouldPreferSendOverCompletion &&
      (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && !mobile))
    ) {
      e.preventDefault();
      return true;
    }
    if (open && matches.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setIndex((i) => (i + 1) % matches.length);
        return true;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setIndex((i) => (i <= 0 ? matches.length - 1 : i - 1));
        return true;
      }
      if (
        !shouldPreferSendOverCompletion &&
        (e.key === "Tab" ||
          (e.key === "Enter" && !e.shiftKey && (!mobile || mobileEnterCompletes))) &&
        index >= 0
      ) {
        e.preventDefault();
        (e.key === "Tab" ? onTabComplete : onSelect)(matches[index]!);
        return true;
      }
    }
    return false;
  }

  function onSelectionChange(element: HTMLTextAreaElement) {
    const next = { text: element.value, start: element.selectionStart, end: element.selectionEnd };
    setSelection((previous) =>
      previous?.text === next.text && previous.start === next.start && previous.end === next.end
        ? previous
        : next,
    );
  }

  // Restore selection after React commits the completed draft, before another input event.
  useLayoutEffect(() => {
    const pending = pendingCaret.current;
    if (!pending) return;
    if (text !== pending.text) {
      if (text !== pending.originalText) pendingCaret.current = null;
      return;
    }
    pendingCaret.current = null;
    const element = textareaRef?.current;
    if (!element || element.value !== pending.text) return;
    element.focus();
    element.setSelectionRange(pending.caret, pending.caret);
    onSelectionChange(element);
  });

  return {
    open,
    query,
    matches,
    index,
    pendingCompletion,
    handleKey,
    commands: menuCommands,
    inline,
    builtinNames,
    onSelectionChange,
    complete: (cmd) => {
      const suffix = text.slice(end);
      const separator = /^\s/.test(suffix) ? "" : " ";
      const completedText = text.slice(0, start) + cmd + separator + suffix;
      // Leave existing newlines and tabs after the caret.
      const advancePastSpace = separator.length > 0 || suffix.startsWith(" ");
      const completedCaret = start + cmd.length + (advancePastSpace ? 1 : 0);
      // Keep completion and caret state together before the textarea restores its selection.
      setSelection({ text: completedText, start: completedCaret, end: completedCaret });
      setDismissed({ text: completedText, start, end: start + cmd.length });
      pendingCaret.current = { text: completedText, originalText: text, caret: completedCaret };
      return { text: completedText, caret: completedCaret };
    },
  };
}
