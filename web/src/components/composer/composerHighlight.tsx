import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from "react";
import { TerminalIcon } from "lucide-react";
import { create } from "zustand";
import { matchSlashCommandInvocation } from "@/components/SlashCommandMenu";
import { COMMAND_TOKEN_CLASS } from "@/components/composer/commandToken";
import type {
  ComposerInputEvent,
  ComposerTextTarget,
  ComposerTokenAt,
} from "@/components/composer/engine/composerInput";
import {
  ComposerMirror,
  MIRRORED_TEXTAREA_CLASS,
  renderMentionTokens,
  useComposerMirrorAlignment,
} from "@/components/composer/MentionTokenOverlay";
import { findMentionTokens } from "@/lib/composerMentions";
import { supportsNativeShellInput } from "@/lib/harnessSetup";
import { isOwnerLevel } from "@/lib/permissionsApi";

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

// The claude-native paste encoder (claude_native/bridge.py `_paste_payload_bytes`)
// drops these before the TUI sees the text; it keeps \t, \n, \r, DEL and C1.
// eslint-disable-next-line no-control-regex
const CLAUDE_DROPPED_CONTROLS = /[\x00-\x08\x0b\x0c\x0e-\x1f]/g;

/** The text Claude Code's TUI receives for a claude-native send of `value`. */
function claudeDeliveredText(value: string): string {
  return value.replace(CLAUDE_DROPPED_CONTROLS, "");
}

/**
 * Split a `!command` draft at its `!`, or null when *harness* would treat it
 * as model input. codex-native mirrors `shell_command_from_content`
 * (codex_native/shell_input.py): leading whitespace ignored, `!!` escapes.
 * claude-native pastes the text into the Claude Code TUI, which enters bash
 * mode on a leading `!`, so only a `!` at the very start of the delivered
 * text counts (`!!` too).
 * Either way the command must be non-blank.
 */
export function splitNativeShellCommand(
  value: string,
  harness: string,
): { before: string; token: string; after: string } | null {
  if (harness === "claude-native") {
    const delivered = claudeDeliveredText(value);
    if (!delivered.startsWith("!") || delivered.slice(1).trim() === "") return null;
    const at = value.indexOf("!");
    return { before: value.slice(0, at), token: "!", after: value.slice(at + 1) };
  }
  const before = value.match(LEADING_PYTHON_WHITESPACE)?.[0] ?? "";
  const rest = value.slice(before.length);
  if (!rest.startsWith("!") || rest.startsWith("!!")) return null;
  if (PYTHON_WHITESPACE_ONLY.test(rest.slice(1))) return null;
  return { before, token: "!", after: rest.slice(1) };
}

/**
 * The harness whose `!` rule applies to this viewer's sends, else null. The
 * server runs codex `!` input only for the owner.
 */
export function nativeShellHarness(
  harness: string | null | undefined,
  permissionLevel: number | null,
): string | null {
  if (!harness || !supportsNativeShellInput(harness)) return null;
  return harness !== "codex-native" || isOwnerLevel(permissionLevel) ? harness : null;
}

export const SHELL_MENTION_REFUSAL =
  "Shell commands can't include @mentions; remove them or drop the leading `!`.";
export const SHELL_REVIEW_COMMENT_REFUSAL =
  "Shell commands can't carry review comments; remove them or drop the leading `!`.";

/** Why a `!` send must not go out, or null: tags and review comments would reach the shell. */
export function shellSendRefusal(
  kind: ComposerHighlight["kind"],
  context: { mentions: readonly unknown[]; reviewComments?: readonly unknown[] },
): string | null {
  if (kind !== "shell") return null;
  if (context.mentions.length > 0) return SHELL_MENTION_REFUSAL;
  return (context.reviewComments?.length ?? 0) > 0 ? SHELL_REVIEW_COMMENT_REFUSAL : null;
}

/** What the overlay tints in `value`; `nativeShell` names the harness whose `!` rule applies. */
export function composerHighlightSplit(
  value: string,
  commands: Iterable<string>,
  nativeShell: string | null,
  classifyValue: string = value,
): ComposerHighlightSplit | null {
  const at = value.indexOf("!");
  if (at >= 0 && nativeShell && splitNativeShellCommand(classifyValue, nativeShell)) {
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
      <span className={COMMAND_TOKEN_CLASS}>{split.token}</span>
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
  /** The text as sent, when it differs from `value`; decides shell mode. */
  classifyValue?: string;
  /** Inline @mention tokens the mirror paints as pills; empty where none apply. */
  mentionTokens: readonly string[];
  /** The textarea the mirror is pinned to. */
  textareaRef: RefObject<ComposerTextTarget | null>;
  /** The input node, when an engine switch can replace it. */
  textareaNode?: ComposerTextTarget | null;
}

export interface ComposerHighlight {
  kind: ComposerHighlightSplit["kind"] | null;
  /** Spread onto the composer textarea (merge `className` with the caller's). */
  input: {
    className: string | undefined;
    /** The same token for the CodeMirror engine, which paints it itself. */
    commandToken: string | ComposerTokenAt | null;
    onScroll: (event: ComposerInputEvent) => void;
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
 * @mention pills, in the composer mirror. One mirror serves both, so a draft
 * holding a command and mentions shows each. Display-only: the draft value is
 * never changed.
 */
export function useComposerHighlight({
  value,
  commandNames,
  enabled,
  nativeShell,
  classifyValue,
  mentionTokens,
  textareaRef,
  textareaNode = null,
}: UseComposerHighlightOptions): ComposerHighlight {
  const mirrorRef = useRef<HTMLDivElement>(null);
  const hintId = useId();
  const split = enabled
    ? composerHighlightSplit(value, commandNames, nativeShell, classifyValue)
    : null;
  const shell = split?.kind === "shell";
  const mirrorActive = split !== null || findMentionTokens(value, mentionTokens).length > 0;
  useComposerMirrorAlignment(mirrorRef, textareaRef, mirrorActive, textareaNode);
  return {
    kind: split?.kind ?? null,
    input: {
      className: mirrorActive ? MIRRORED_TEXTAREA_CLASS : undefined,
      // A command keeps the editor's own leading-word check; `!` needs its exact offset.
      commandToken: shell ? { token: "!", start: split.before.length } : (split?.token ?? null),
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
// Composers sharing a conversation stack up in mount order and the newest still
// mounted wins. A catalog change updates its slot in place, keeping that order.
interface PublishedCommandsSlot {
  current: PublishedComposerCommands;
}
const publishedCommandStacks = new Map<string, PublishedCommandsSlot[]>();

function syncPublishedCommands(conversationId: string): void {
  const top = publishedCommandStacks.get(conversationId)?.at(-1)?.current;
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
  const [slot] = useState<PublishedCommandsSlot>(() => ({
    current: { commandNames, nativeShell },
  }));
  useEffect(() => {
    if (!conversationId) return;
    publishedCommandStacks.set(conversationId, [
      ...(publishedCommandStacks.get(conversationId) ?? []),
      slot,
    ]);
    syncPublishedCommands(conversationId);
    return () => {
      const remaining = (publishedCommandStacks.get(conversationId) ?? []).filter(
        (entry) => entry !== slot,
      );
      if (remaining.length > 0) publishedCommandStacks.set(conversationId, remaining);
      else publishedCommandStacks.delete(conversationId);
      syncPublishedCommands(conversationId);
    };
  }, [conversationId, slot]);
  useEffect(() => {
    const { current } = slot;
    if (current.commandNames === commandNames && current.nativeShell === nativeShell) return;
    slot.current = { commandNames, nativeShell };
    if (conversationId) syncPublishedCommands(conversationId);
  }, [conversationId, slot, commandNames, nativeShell]);
}

export function usePublishedComposerCommands(
  conversationId: string | null | undefined,
): PublishedComposerCommands | undefined {
  return usePublishedCommandsStore((state) => (conversationId ? state[conversationId] : undefined));
}
