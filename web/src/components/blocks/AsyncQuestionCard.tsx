import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ConversationScopeContext } from "@/components/chat/conversationScope";
import { Button } from "@/components/ui/button";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import {
  asyncQuestions,
  readAsyncQuestionReply,
  type AsyncQuestionReply,
  type AsyncUserInput,
} from "@/lib/asyncQuestions";
import { getSessionDraft, setSessionDraft } from "@/lib/sessionDrafts";
import { useChatStore } from "@/store/chatStore";
import {
  AskUserQuestionForm,
  type AskUserQuestionAnswers,
  type AskUserQuestionDraft,
} from "./AskUserQuestionForm";

interface SavedQuestion {
  draft?: AskUserQuestionDraft;
  reply?: AsyncQuestionReply;
}

function readSavedQuestion(key: string): SavedQuestion {
  try {
    const raw: unknown = JSON.parse(getSessionDraft(key)?.text ?? "{}");
    if (!raw || typeof raw !== "object") return {};
    const saved = raw as SavedQuestion;
    const draft = saved.draft;
    if (
      draft &&
      (!Number.isInteger(draft.currentIndex) ||
        !draft.selections ||
        !draft.customInputs ||
        !draft.customSelected ||
        !Object.values(draft.selections).every(
          (v) =>
            typeof v === "string" || (Array.isArray(v) && v.every((s) => typeof s === "string")),
        ) ||
        !Object.values(draft.customInputs).every((v) => typeof v === "string") ||
        !Object.values(draft.customSelected).every((v) => typeof v === "boolean"))
    )
      return {};
    return { draft, reply: readAsyncQuestionReply(saved.reply) };
  } catch {
    return {};
  }
}

/** Async questions are messages, not pending approval RPCs. */
export function AsyncQuestionCard({ itemId, prompt }: { itemId: string; prompt: AsyncUserInput }) {
  const scope = useContext(ConversationScopeContext);
  const activeId = useChatStore((s) => s.conversationId);
  const sessionId = scope ?? activeId;
  const state = useConversationEntryState(sessionId);
  const key = `async-question:${sessionId}:${itemId}`;
  const saved = useRef(readSavedQuestion(key));
  const [reply, setReply] = useState(saved.current.reply);
  const [sending, setSending] = useState(false);
  const inFlight = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const questions = useMemo(() => asyncQuestions(prompt), [prompt]);
  const completed = state.blocks.find(
    (b) => b.type === "user_message" && b.asyncQuestionReply?.source_item_id === itemId,
  );
  const confirmed = completed?.type === "user_message" ? completed.asyncQuestionReply : undefined;
  const save = useCallback(
    () => setSessionDraft(key, { text: JSON.stringify(saved.current), files: [] }),
    [key],
  );
  const saveDraft = useCallback(
    (draft: AskUserQuestionDraft) => {
      saved.current.draft = draft;
      save();
    },
    [save],
  );
  useEffect(() => {
    if (confirmed) setSessionDraft(key, { text: "", files: [] });
  }, [confirmed, key]);
  const initialDraft = saved.current.draft ?? {
    currentIndex: 0,
    selections: Object.fromEntries(questions.map((q) => [q.id!, q.options[0]?.label ?? ""])),
    customSelected: {},
    customInputs: {},
  };
  const sendReply = async (answer: AsyncQuestionReply) => {
    if (inFlight.current || !sessionId || !state.boundAgentId || confirmed) return;
    inFlight.current = true;
    setSending(true);
    setError(null);
    saved.current.reply = answer;
    save();
    setReply(answer);
    try {
      const stableId = prompt.reply_id;
      const text = answer.cancelled
        ? "I dismissed these questions without selecting an answer."
        : `My answers to your questions:\n\n${questions.map((q) => `${q.question}\n${answer.answers[q.id!]}`).join("\n\n")}`;
      await useChatStore.getState().send(text, state.boundAgentId, [], {
        pinnedConversationId: sessionId,
        stableId,
        asyncQuestionReply: answer,
        onError: (message) => setError(message),
      });
    } catch {
      setError("The reply was not confirmed. Retry sends the same answer safely.");
    } finally {
      inFlight.current = false;
      setSending(false);
    }
  };
  const submit = (answers: AskUserQuestionAnswers) => {
    void sendReply({
      source_item_id: itemId,
      answers: Object.fromEntries(
        Object.entries(answers).map(([id, value]) => [
          id,
          Array.isArray(value) ? value.join(", ") : value,
        ]),
      ),
      cancelled: false,
    });
  };
  const shown = confirmed ?? reply;
  const unavailable = !sessionId || !state.boundAgentId;
  return (
    <section
      data-testid="async-question-card"
      data-source-item-id={itemId}
      className="my-3 rounded-md border p-3 text-sm"
      aria-label="Questions"
    >
      <p className="mb-2 font-medium">Questions</p>
      {shown ? (
        <>
          <p role="status">
            {confirmed
              ? shown.cancelled
                ? "Cancelled without an answer"
                : "Answer submitted"
              : sending
                ? "Sending reply…"
                : error
                  ? "Reply not confirmed"
                  : "Reply queued; waiting for confirmation"}
          </p>
          {!shown.cancelled && (
            <dl>
              {questions.map((q) => (
                <div key={q.id}>
                  <dt>{q.question}</dt>
                  <dd className="text-muted-foreground">{shown.answers[q.id!]}</dd>
                </div>
              ))}
            </dl>
          )}
          {!confirmed && (
            <Button
              size="sm"
              variant="outline"
              disabled={sending || unavailable}
              onClick={() => void sendReply(shown)}
            >
              Retry delivery
            </Button>
          )}
        </>
      ) : (
        <fieldset disabled={sending || unavailable}>
          <AskUserQuestionForm
            questions={questions}
            initialDraft={initialDraft}
            onDraftChange={saveDraft}
            onSubmit={submit}
            onReject={() =>
              void sendReply({ source_item_id: itemId, answers: {}, cancelled: true })
            }
          />
        </fieldset>
      )}
      {unavailable && !confirmed && <p>This chat cannot accept replies right now.</p>}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
