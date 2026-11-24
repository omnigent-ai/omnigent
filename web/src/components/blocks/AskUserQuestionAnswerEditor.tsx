// The custom-answer field on the CodeMirror prompt editor, with the chat composer's @-session
// menu. AnswerMentionScope owns the session tags above the questions, so they outlive a question.

import {
  type Dispatch,
  type ReactNode,
  type RefObject,
  type SetStateAction,
  useEffect,
  useState,
} from "react";
import { ComposerEngineContext, ComposerEngineInput } from "@/components/composer/ChatComposer";
import type { ComposerTextTarget } from "@/components/composer/engine/composerInput";
import { FileMentionMenu } from "@/components/FileMentionMenu";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { type MentionBrowser, useMentionBrowser } from "@/hooks/useMentionBrowser";
import {
  MENTION_SEARCH_DEBOUNCE_MS,
  useSessionMentionEntries,
} from "@/hooks/useSessionMentionEntries";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import {
  composerInlineMentions,
  detectMentionAt,
  expandMentionTokens,
  presentMentions,
  withSessionMentionHint,
  type MentionState,
  type SessionMention,
} from "@/lib/composerMentions";

export const NO_FILE_ENTRIES: WorkspaceFile[] = [];

/**
 * An answer's text as the agent receives it: each tagged session becomes the marker the chat
 * composer sends, with its tool hint. Answers are plain text, so files are not tagged here.
 */
export function expandAnswerMentions(text: string, sessions: readonly SessionMention[]): string {
  const mentions = presentMentions(composerInlineMentions([], sessions, null), [text]);
  return withSessionMentionHint(expandMentionTokens(text, mentions), mentions.length > 0);
}

/** The @-mention state the custom answer's editor and menu share. */
export interface AnswerMentions {
  browser: MentionBrowser;
  mention: MentionState | null;
  setMention: (next: MentionState | null) => void;
  sessionEntries: readonly SessionMention[];
}

/**
 * Holds the custom answers' session tags on the prompt editor path. It sits above the questions,
 * so a tag survives a question change, and mirrors the tags out for submit and the draft.
 */
export function AnswerMentionScope({
  currentKey,
  customText,
  onCustomTextChange,
  segments,
  initialSessions,
  isMobile,
  inputRef,
  onSessionsChange,
  children,
}: {
  currentKey: string;
  customText: string;
  onCustomTextChange: (text: string) => void;
  /** Every answer's text; a tag whose token is in none of them is released. */
  segments: readonly string[];
  initialSessions: SessionMention[];
  isMobile: boolean;
  inputRef: RefObject<ComposerTextTarget | null>;
  onSessionsChange: Dispatch<SetStateAction<SessionMention[]>>;
  children: (mentions: AnswerMentions) => ReactNode;
}) {
  // The open menu belongs to one question.
  const [typedMention, setTypedMention] = useState<{ key: string; state: MentionState } | null>(
    null,
  );
  const mention = typedMention?.key === currentKey ? typedMention.state : null;
  const setMention = (next: MentionState | null) =>
    setTypedMention(next ? { key: currentKey, state: next } : null);
  const debouncedQuery = useDebouncedValue(mention?.query ?? "", MENTION_SEARCH_DEBOUNCE_MS);
  const sessionEntries = useSessionMentionEntries({
    mention,
    debouncedQuery,
    currentSessionId: null,
  });
  // The hook releases a tag once its token is gone from every answer, and an undo brings it back.
  const browser = useMentionBrowser({
    mention,
    setMention,
    mentionEntries: NO_FILE_ENTRIES,
    sessionEntries,
    text: customText,
    setText: onCustomTextChange,
    textareaRef: inputRef,
    isMobile,
    initialMentions: { files: [], sessions: initialSessions },
    segments,
  });
  const sessions = browser.mentionedSessions;
  useEffect(() => onSessionsChange(sessions), [sessions, onSessionsChange]);
  return <>{children({ browser, mention, setMention, sessionEntries })}</>;
}

interface AskUserQuestionAnswerEditorProps {
  inputRef: RefObject<ComposerTextTarget | null>;
  value: string;
  onValueChange: (text: string) => void;
  mentions: AnswerMentions;
}

export function AskUserQuestionAnswerEditor({
  inputRef,
  value,
  onValueChange,
  mentions,
}: AskUserQuestionAnswerEditorProps) {
  const { browser, mention, setMention, sessionEntries } = mentions;
  const {
    mentionIndex,
    mentionOpen,
    sessionMenu,
    mentionTokens,
    attachSession,
    attachMention,
    openMentionDir,
    noteTextChange,
    handleKeyDown,
    handleTokenBackspace,
    dismiss,
  } = browser;

  return (
    <div
      className="relative min-w-0 flex-1"
      data-suggestions-open={mentionOpen ? "true" : undefined}
    >
      <ComposerEngineContext.Provider value="codemirror">
        <ComposerEngineInput
          keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
          input={{
            ref: inputRef,
            value,
            placeholder: "Type something",
            "aria-label": "Custom answer",
            "data-testid": "ask-user-question-custom-input",
            heightMode: "unbounded",
            className: "min-w-0 flex-1",
            mentionTokens,
            onChange: (event) => {
              noteTextChange(event);
              const { value: text, selectionStart } = event.currentTarget;
              onValueChange(text);
              setMention(
                detectMentionAt(
                  text,
                  selectionStart,
                  sessionEntries.map((session) => session.title),
                ),
              );
            },
            onKeyDown: (event) => {
              if (!handleKeyDown(event)) handleTokenBackspace(event);
            },
            onBlur: dismiss,
          }}
        />
      </ComposerEngineContext.Provider>
      {mentionOpen && (
        <FileMentionMenu
          currentDir=""
          query={mention?.query ?? ""}
          activeIndex={mentionIndex}
          entries={NO_FILE_ENTRIES}
          onOpenDir={openMentionDir}
          onAttach={attachMention}
          sessionMenu={sessionMenu}
          onAttachSession={attachSession}
        />
      )}
    </div>
  );
}
