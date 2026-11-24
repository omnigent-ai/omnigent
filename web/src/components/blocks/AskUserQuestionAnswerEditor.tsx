// The custom answer of one question: the chat composer's @-session menu, tracked per question.
// AnswerMentionScope runs on both engines, so a textarea edit is tracked like an editor edit.

import {
  type Dispatch,
  type ReactNode,
  type RefObject,
  type SetStateAction,
  useEffect,
  useState,
} from "react";
import { useScopedConversationId } from "@/components/chat/conversationScope";
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
 * composer sends, with its tool hint. Only the tags of that answer are considered.
 */
export function expandAnswerMentions(text: string, sessions: readonly SessionMention[]): string {
  const mentions = presentMentions(
    composerInlineMentions({ files: [], sessions: [...sessions] }, null),
    [text],
  );
  return withSessionMentionHint(expandMentionTokens(text, mentions), mentions.length > 0);
}

/** One question's mention state, handed to its editor. */
export interface AnswerMentions {
  browser: MentionBrowser;
  mention: MentionState | null;
  setMention: (next: MentionState | null) => void;
  debouncedQuery: string;
  sessionEntries: readonly SessionMention[];
  onSessionEntries: Dispatch<SetStateAction<readonly SessionMention[]>>;
}

/**
 * Holds one question's session tags on both engines. It is keyed to the question, so it starts from
 * that question's current tags, and it writes the tags back to the form's per-question record.
 */
export function AnswerMentionScope({
  currentKey,
  customText,
  onCustomTextChange,
  initialSessions,
  onSessionsChange,
  isMobile,
  inputRef,
  children,
}: {
  currentKey: string;
  customText: string;
  onCustomTextChange: (text: string) => void;
  /** This question's tags as the form holds them now. */
  initialSessions: SessionMention[];
  onSessionsChange: Dispatch<SetStateAction<Record<string, SessionMention[]>>>;
  isMobile: boolean;
  inputRef: RefObject<ComposerTextTarget | null>;
  children: (mentions: AnswerMentions) => ReactNode;
}) {
  const [typedMention, setTypedMention] = useState<{ key: string; state: MentionState } | null>(
    null,
  );
  const mention = typedMention?.key === currentKey ? typedMention.state : null;
  const setMention = (next: MentionState | null) =>
    setTypedMention(next ? { key: currentKey, state: next } : null);
  const debouncedQuery = useDebouncedValue(mention?.query ?? "", MENTION_SEARCH_DEBOUNCE_MS);
  // The editor lists sessions and reports them here; the listing is empty off the editor path.
  const [sessionEntries, setSessionEntries] = useState<readonly SessionMention[]>([]);
  // The hook releases a tag once its token is gone from this answer, and an undo brings it back.
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
    segments: [customText],
  });
  const sessions = browser.mentionedSessions;
  useEffect(
    () => onSessionsChange((prev) => ({ ...prev, [currentKey]: sessions })),
    [currentKey, sessions, onSessionsChange],
  );
  return (
    <>
      {children({
        browser,
        mention,
        setMention,
        debouncedQuery,
        sessionEntries,
        onSessionEntries: setSessionEntries,
      })}
    </>
  );
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
  const { browser, mention, setMention, debouncedQuery, onSessionEntries } = mentions;
  // The session search needs the app's query client, so it runs only while the editor is mounted.
  // The session's own row is left out, as the chat composer leaves it out.
  const currentSessionId = useScopedConversationId();
  const sessionEntries = useSessionMentionEntries({ mention, debouncedQuery, currentSessionId });
  useEffect(() => onSessionEntries(sessionEntries), [sessionEntries, onSessionEntries]);
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
