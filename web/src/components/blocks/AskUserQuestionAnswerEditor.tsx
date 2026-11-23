// The custom-answer field on the CodeMirror prompt editor, with the chat composer's @-session
// menu. Rendered only on that engine, so the textarea path never mounts its session search.

import type { Dispatch, RefObject, SetStateAction } from "react";
import { useState } from "react";
import { ComposerEngineContext, ComposerEngineInput } from "@/components/composer/ChatComposer";
import type { ComposerTextTarget } from "@/components/composer/engine/composerInput";
import { FileMentionMenu } from "@/components/FileMentionMenu";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useMentionBrowser } from "@/hooks/useMentionBrowser";
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

const NO_FILE_ENTRIES: WorkspaceFile[] = [];

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
  /** Sessions tagged in any question's answer; shared across the card's questions. */
  sessions: SessionMention[];
  onSessionsChange: Dispatch<SetStateAction<SessionMention[]>>;
}

export function AskUserQuestionAnswerEditor({
  inputRef,
  value,
  onValueChange,
  sessions,
  onSessionsChange,
}: AskUserQuestionAnswerEditorProps) {
  const [mention, setMention] = useState<MentionState | null>(null);
  const debouncedQuery = useDebouncedValue(mention?.query ?? "", MENTION_SEARCH_DEBOUNCE_MS);
  const sessionEntries = useSessionMentionEntries({
    mention,
    debouncedQuery,
    currentSessionId: null,
  });
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
  } = useMentionBrowser({
    mention,
    setMention,
    mentionEntries: NO_FILE_ENTRIES,
    sessionEntries,
    text: value,
    setText: onValueChange,
    textareaRef: inputRef,
    tags: {
      mentions: { files: [], sessions },
      updateMentions: (update) =>
        onSessionsChange((current) => update({ files: [], sessions: current }).sessions),
    },
  });

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
