// The custom-answer field on the CodeMirror prompt editor, with the chat composer's @-session
// menu. The form owns the mention state; this field only lists sessions and reports them up.

import { type Dispatch, type SetStateAction, useEffect, type RefObject } from "react";
import { ComposerEngineContext, ComposerEngineInput } from "@/components/composer/ChatComposer";
import type { ComposerTextTarget } from "@/components/composer/engine/composerInput";
import { FileMentionMenu } from "@/components/FileMentionMenu";
import type { MentionBrowser } from "@/hooks/useMentionBrowser";
import { useSessionMentionEntries } from "@/hooks/useSessionMentionEntries";
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

interface AskUserQuestionAnswerEditorProps {
  inputRef: RefObject<ComposerTextTarget | null>;
  value: string;
  onValueChange: (text: string) => void;
  mention: MentionState | null;
  setMention: (next: MentionState | null) => void;
  /** The mention query after the form's debounce; the session search runs on it. */
  debouncedQuery: string;
  /** Reports this editor's session rows so the form's mention browser can navigate them. */
  onSessionEntries: Dispatch<SetStateAction<readonly SessionMention[]>>;
  browser: MentionBrowser;
}

export function AskUserQuestionAnswerEditor({
  inputRef,
  value,
  onValueChange,
  mention,
  setMention,
  debouncedQuery,
  onSessionEntries,
  browser,
}: AskUserQuestionAnswerEditorProps) {
  // The session search needs the app's query client, so it runs only while the editor is mounted.
  const sessionEntries = useSessionMentionEntries({
    mention,
    debouncedQuery,
    currentSessionId: null,
  });
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
