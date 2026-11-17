import { useEffect, useId, useRef, type ReactNode, type RefObject, type UIEvent } from "react";
import { TerminalIcon } from "lucide-react";
import { create } from "zustand";
import { matchSlashCommandInvocation } from "@/components/SlashCommandMenu";
import {
  ComposerMirror,
  MIRRORED_TEXTAREA_CLASS,
  renderMentionTokens,
  useComposerMirrorAlignment,
} from "@/components/composer/MentionTokenOverlay";
import { findMentionTokens } from "@/lib/composerMentions";

export interface ComposerHighlightSplit {
  kind: "command" | "shell";
  before: string;
  token: string;
  after: string;
}

/**
 * Splits a draft that invokes a known command or skill into the tinted token
 * and the rest. There is no command-shaped fallback: unknown /words stay plain
 * so the tint never claims a command that won't run.
 */
export function splitKnownSlashCommand(
  value: string,
  commands: Iterable<string>,
): { before: string; token: string; after: string } | null {
  const token = matchSlashCommandInvocation(value, commands)?.command;
  if (token === undefined) return null;
  const before = value.match(/^\s*/)?.[0] ?? "";
  return { before, token, after: value.slice(before.length + token.length) };
}

// Python's `str.isspace` set, which the backend's strip()s use. JS `\s` lacks
// \x1c-\x1f and \x85 and adds \ufeff, so it disagrees on those drafts.
const PYTHON_WHITESPACE_CLASS =
  "\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const LEADING_PYTHON_WHITESPACE = new RegExp(`^[${PYTHON_WHITESPACE_CLASS}]*`);
const PYTHON_WHITESPACE_ONLY = new RegExp(`^[${PYTHON_WHITESPACE_CLASS}]*$`);

/**
 * Split a `!command` draft at its `!`, or null when *harness* would treat it
 * as model input. codex-native mirrors `shell_command_from_content`
 * (codex_native/shell_input.py): leading whitespace ignored, `!!` escapes.
 * claude-native pastes the text into the Claude Code TUI, which enters bash
 * mode on a leading `!`, so only a `!` at the very start counts (`!!` too).
 * Either way the command must be non-blank.
 */
export function splitNativeShellCommand(
  value: string,
  harness: string,
): { before: string; token: string; after: string } | null {
  if (harness === "claude-native") {
    if (!value.startsWith("!") || value.slice(1).trim() === "") return null;
    return { before: "", token: "!", after: value.slice(1) };
  }
  const before = value.match(LEADING_PYTHON_WHITESPACE)?.[0] ?? "";
  const rest = value.slice(before.length);
  if (!rest.startsWith("!") || rest.startsWith("!!")) return null;
  if (PYTHON_WHITESPACE_ONLY.test(rest.slice(1))) return null;
  return { before, token: "!", after: rest.slice(1) };
}

/**
 * What the composer overlay tints in `value`. `nativeShell` is the harness
 * whose `!` rule applies when a `!command` from this viewer runs as a shell
 * command in the session, else null.
 */
export function composerHighlightSplit(
  value: string,
  commands: Iterable<string>,
  nativeShell: string | null,
  classifyValue: string = value,
): ComposerHighlightSplit | null {
  if (nativeShell && splitNativeShellCommand(classifyValue, nativeShell)) {
    // classifyValue only drops characters and never a `!`, so the token is value's first `!`.
    const at = value.indexOf("!");
    return { kind: "shell", before: value.slice(0, at), token: "!", after: value.slice(at + 1) };
  }
  const command = splitKnownSlashCommand(value, commands);
  return command ? { kind: "command", ...command } : null;
}

/** Mirror content: the tinted token, then the rest with its @mention pills. */
export function renderComposerHighlight(
  value: string,
  split: ComposerHighlightSplit | null,
  mentionTokens: readonly string[],
): ReactNode {
  if (!split) return renderMentionTokens(value, mentionTokens);
  return (
    <>
      {split.before}
      <span className="text-brand-accent">{split.token}</span>
      {renderMentionTokens(split.after, mentionTokens)}
    </>
  );
}

export interface UseComposerHighlightOptions {
  value: string;
  commandNames: readonly string[];
  /** False while quotes or attachments ride along (they change what is sent). */
  enabled: boolean;
  nativeShell: string | null;
  /** The text as sent, when sending drops characters from `value`; decides shell mode. */
  classifyValue?: string;
  /** Inline @mention tokens the mirror paints as pills; empty where none apply. */
  mentionTokens: readonly string[];
  /** The textarea the mirror is pinned to. */
  textareaRef: RefObject<HTMLTextAreaElement | null>;
}

export interface ComposerHighlight {
  kind: ComposerHighlightSplit["kind"] | null;
  /** Spread onto the composer textarea (merge `className` with the caller's). */
  input: {
    className: string | undefined;
    onScroll: (event: UIEvent<HTMLTextAreaElement>) => void;
    "aria-describedby": string | undefined;
    "data-slash-command": "true" | undefined;
    "data-shell-command": "true" | undefined;
  };
  cardClassName: string | undefined;
  /** The composer's single `ComposerMirror`, for `ChatComposer`'s `inputBackdrop` slot. */
  backdrop: ReactNode;
  hint: ReactNode;
}

/**
 * Paint a leading known command or native `!` shell token, plus inline
 * @mention pills, in upstream's composer mirror. One mirror serves both, so a
 * draft holding a command and mentions shows each. Display-only: the draft
 * value is never changed.
 */
export function useComposerHighlight({
  value,
  commandNames,
  enabled,
  nativeShell,
  classifyValue,
  mentionTokens,
  textareaRef,
}: UseComposerHighlightOptions): ComposerHighlight {
  const mirrorRef = useRef<HTMLDivElement>(null);
  const hintId = useId();
  const split = enabled
    ? composerHighlightSplit(value, commandNames, nativeShell, classifyValue)
    : null;
  const shell = split?.kind === "shell";
  const mirrorActive = split !== null || findMentionTokens(value, mentionTokens).length > 0;
  useComposerMirrorAlignment(mirrorRef, textareaRef, mirrorActive);
  return {
    kind: split?.kind ?? null,
    input: {
      className: mirrorActive ? MIRRORED_TEXTAREA_CLASS : undefined,
      onScroll: (event) => {
        if (mirrorRef.current) mirrorRef.current.scrollTop = event.currentTarget.scrollTop;
      },
      "aria-describedby": shell ? hintId : undefined,
      "data-slash-command": split?.kind === "command" ? "true" : undefined,
      "data-shell-command": shell ? "true" : undefined,
    },
    // The surface's unlayered border-color outranks utilities; a ring does not.
    cardClassName: shell ? "ring-1 ring-inset ring-brand-accent" : undefined,
    backdrop: mirrorActive && (
      <ComposerMirror ref={mirrorRef}>
        {renderComposerHighlight(value, split, mentionTokens)}
      </ComposerMirror>
    ),
    // The live region stays mounted so screen readers announce the text change.
    hint: (
      <>
        <span aria-live="polite" data-testid="composer-shell-mode-status" className="sr-only">
          {shell ? "Shell command" : ""}
        </span>
        {shell && (
          <div
            id={hintId}
            data-testid="composer-shell-mode-indicator"
            className="flex items-center gap-1 pt-1 text-xs text-brand-accent"
          >
            <TerminalIcon className="size-3" aria-hidden />
            Shell command
          </div>
        )}
      </>
    ),
  };
}

export interface PublishedComposerCommands {
  commandNames: readonly string[];
  nativeShell: string | null;
}

// Sent bubbles render outside the composer's tree, so each composer publishes
// its known commands for the conversation it sends to.
const usePublishedCommandsStore = create<Record<string, PublishedComposerCommands>>(() => ({}));
// Composers sharing a conversation stack up; the newest still mounted wins, so
// unmounting in either order leaves the survivor's commands published.
const publishedCommandStacks = new Map<string, PublishedComposerCommands[]>();

function syncPublishedCommands(conversationId: string): void {
  const top = publishedCommandStacks.get(conversationId)?.at(-1);
  usePublishedCommandsStore.setState((state) => {
    const { [conversationId]: _previous, ...rest } = state;
    return top ? { ...rest, [conversationId]: top } : rest;
  }, true);
}

export function usePublishComposerCommands(
  conversationId: string | null | undefined,
  commandNames: readonly string[],
  nativeShell: string | null,
): void {
  useEffect(() => {
    if (!conversationId) return;
    const published = { commandNames, nativeShell };
    const stack = publishedCommandStacks.get(conversationId) ?? [];
    publishedCommandStacks.set(conversationId, [...stack, published]);
    syncPublishedCommands(conversationId);
    return () => {
      const remaining = (publishedCommandStacks.get(conversationId) ?? []).filter(
        (entry) => entry !== published,
      );
      if (remaining.length > 0) publishedCommandStacks.set(conversationId, remaining);
      else publishedCommandStacks.delete(conversationId);
      syncPublishedCommands(conversationId);
    };
  }, [conversationId, commandNames, nativeShell]);
}

export function usePublishedComposerCommands(
  conversationId: string | null | undefined,
): PublishedComposerCommands | undefined {
  return usePublishedCommandsStore((state) => (conversationId ? state[conversationId] : undefined));
}
