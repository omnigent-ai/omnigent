// Slash-completion menu mechanics shared by the composer surfaces: when the
// draft reads as a lone command token ("/rev", "$review"), the menu opens
// with ranked matches and owns the keys that navigate, complete, or dismiss
// it. What a selection DOES (fill the draft, execute a command) and where
// the command inventory comes from stay with the calling surface.

import { useState } from "react";
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
  /**
   * The command prefix this surface's inventory uses ("$" for codex-native,
   * "/" otherwise). "/" always opens the menu as well.
   */
  prefix: string;
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
   * When true, Escape clears the draft only if the menu has content (matches,
   * or discovery still in flight), so an idle Escape can fall through to
   * other handlers. When false, Escape always clears while the menu is open.
   */
  escapeClearsOnlyWithContent: boolean;
  /** Surface-level gate ANDed with the token shape (focus, attachments). */
  allowOpen: boolean;
  /** Called with the ranked, prefixed name chosen via Tab/Enter. */
  onSelect: (cmd: string) => void;
  /** Clears the composer draft (Escape semantics). */
  clearText: () => void;
}

export interface UseSlashCompletionResult {
  /** Whether the suggestions menu is open. */
  open: boolean;
  /** The text typed after the prefix while open, else "". */
  query: string;
  /** Ranked match names while open, else []. */
  matches: string[];
  /** Highlighted row index, -1 when nothing is highlighted. */
  index: number;
  /**
   * The draft reads as a lone command token, discovery is in flight, and
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
  prefix,
  status,
  mobile,
  mobileEnterCompletes,
  escapeClearsOnlyWithContent,
  allowOpen,
  onSelect,
  clearText,
}: UseSlashCompletionOptions): UseSlashCompletionResult {
  const trimmed = text.trimStart();
  // "/" always opens; a surface whose inventory uses another prefix
  // ("$review" for codex-native) opens on that prefix too — even while the
  // inventory is still empty (discovery in flight).
  const hasCommandPrefix = trimmed.startsWith("/") || trimmed.startsWith(prefix);
  // Suggest names until a space starts the arguments; exclude file paths.
  const baseOpen = hasCommandPrefix && !trimmed.slice(1).includes("/") && !trimmed.includes(" ");
  const open = allowOpen && baseOpen;
  // Ranked on the token shape alone: a pending completion still reports
  // while the menu is blurred closed, since submit gating keys off it.
  // The returned query/matches stay gated on open.
  const baseQuery = baseOpen ? trimmed.slice(1) : "";
  // Kept in sync with what the menu renders so keyboard nav indexes into
  // the same list.
  const baseMatches = baseOpen ? rankedSlashCommandNames(commands, baseQuery) : [];
  const query = open ? baseQuery : "";
  const matches = open ? baseMatches : [];
  const pendingCompletion = baseOpen && status === "loading" && baseMatches.length === 0;

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
      clearText();
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
        onSelect(matches[index]!);
        return true;
      }
    }
    return false;
  }

  return { open, query, matches, index, pendingCompletion, handleKey };
}
