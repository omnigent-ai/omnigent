import { useMemo, useState } from "react";
import { useParams } from "@/lib/routing";
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  Loader2Icon,
  SendIcon,
  ShieldCheckIcon,
} from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  buildStakeholderResponseArtifact,
  parseStakeholderResponseText,
} from "@/lib/dpia/requestArtifacts";
import {
  buildContributorAgentMessage,
  displayMessageFromWireText,
  DPIA_RESPONSE_STATUS_LABEL,
  scopedQuestionsFromIntro,
  setDpiaSessionLabels,
} from "@/lib/dpia/requestSession";
import { bubbleText, useDpiaSessionChat } from "./useDpiaSessionChat";

export function DpiaRespondPage() {
  const { sessionId = "" } = useParams<{ sessionId: string }>();
  const chat = useDpiaSessionChat(sessionId || undefined);
  const [name, setName] = useState("");
  const [team, setTeam] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [reviewOpen, setReviewOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submittedLocally, setSubmittedLocally] = useState(false);

  const transcriptTexts = useMemo(
    () => [...chat.historyBubbles, ...chat.liveBubbles].map(bubbleText).filter(Boolean),
    [chat.historyBubbles, chat.liveBubbles],
  );
  const intro = useMemo(() => {
    for (const text of transcriptTexts) {
      const questions = scopedQuestionsFromIntro(text);
      if (questions) {
        const caseMatch = text.match(/case "([^"]+)" \(([^)]+)\)/);
        const requestMatch = text.match(/request_id "([^"]+)"/);
        return {
          questions,
          caseTitle: caseMatch?.[1] ?? "DPIA case",
          caseId: caseMatch?.[2] ?? "",
          requestId: requestMatch?.[1],
        };
      }
    }
    return null;
  }, [transcriptTexts]);
  const submittedResponse = useMemo(() => {
    for (let index = transcriptTexts.length - 1; index >= 0; index -= 1) {
      const parsed = parseStakeholderResponseText(transcriptTexts[index]);
      if (parsed) return parsed;
    }
    return null;
  }, [transcriptTexts]);
  const submitted = submittedLocally || submittedResponse !== null;

  const answersReady =
    intro !== null &&
    name.trim().length >= 2 &&
    team.trim().length >= 2 &&
    intro.questions.every((question) => (answers[question.id] ?? "").trim().length >= 10);

  async function submitResponse() {
    if (!intro || !intro.caseId) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const artifact = buildStakeholderResponseArtifact(
        {
          caseId: intro.caseId,
          requestId: intro.requestId,
          respondentName: name,
          respondentTeam: team,
          answers: intro.questions.map((question) => ({
            questionId: question.id,
            response: answers[question.id] ?? "",
          })),
        },
        new Date().toISOString(),
      );
      const sent = await chat.sendMessage(
        JSON.stringify(artifact),
        "Submitted answers to the Privacy Office.",
      );
      if (!sent) throw new Error("The response message was not accepted.");
      await setDpiaSessionLabels(sessionId, { [DPIA_RESPONSE_STATUS_LABEL]: "submitted" });
      setSubmittedLocally(true);
      setReviewOpen(false);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "The response could not be sent.");
    } finally {
      setSubmitting(false);
    }
  }

  async function sendChatMessage() {
    const text = message.trim();
    if (!text) return;
    const sent = await chat.sendMessage(buildContributorAgentMessage(text), text);
    if (sent) setMessage("");
  }

  return (
    <PageScroll
      maxWidthClassName="max-w-[1100px]"
      contentClassName="px-5 md:px-8"
      data-testid="dpia-respond-page"
    >
      <header className="mb-6 border-b border-border pb-5">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium text-muted-foreground">
          <ShieldCheckIcon className="size-4" />
          Privacy operations
        </div>
        <h1 className="text-2xl font-semibold tracking-normal">Answer the Privacy Office</h1>
        <p className="mt-1 max-w-2xl text-ui text-muted-foreground">
          The Privacy Officer shared scoped questions with you
          {intro ? ` for “${intro.caseTitle}”` : ""}. Answer in the form or discuss with the agent
          first — your confirmed answers go to the officer for review.
        </p>
        <Badge variant="outline" className="mt-3">
          Synthetic data only
        </Badge>
      </header>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,26rem)]">
        <section
          aria-label="Outreach conversation"
          className="flex min-h-[24rem] flex-col rounded-lg border border-border bg-card"
        >
          <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
            <p className="text-sm font-medium">Conversation</p>
            <Badge variant="outline">{submitted ? "Answers submitted" : "Awaiting answers"}</Badge>
          </div>
          <div
            className="flex-1 space-y-3 overflow-y-auto px-4 py-3"
            data-testid="dpia-respond-transcript"
            aria-live="polite"
          >
            {[...chat.historyBubbles, ...chat.liveBubbles].map((bubble) => {
              if (bubble.kind !== "user" && bubble.kind !== "assistant") return null;
              const bubbleKey =
                bubble.kind === "user" ? `u-${bubble.itemId}` : `a-${bubble.stableId}`;
              const raw = bubbleText(bubble);
              if (!raw) return null;
              if (parseStakeholderResponseText(raw)) {
                return (
                  <p
                    key={bubbleKey}
                    className="flex items-center gap-2 text-sm text-muted-foreground"
                  >
                    <CheckCircle2Icon className="size-4 shrink-0" />
                    Structured response submitted for officer review.
                  </p>
                );
              }
              const display = displayMessageFromWireText(raw);
              if (bubble.kind === "user") {
                return display.sender === "officer" ? (
                  <div
                    key={bubbleKey}
                    className="max-w-[85%] rounded-md border border-border px-3 py-2"
                  >
                    <p className="mb-1 text-xs font-medium text-muted-foreground">Privacy Office</p>
                    <p className="text-sm whitespace-pre-wrap">{display.text}</p>
                  </div>
                ) : (
                  <div key={bubbleKey} className="ml-auto max-w-[85%] bg-muted px-3 py-2">
                    <p className="text-sm whitespace-pre-wrap">{display.text}</p>
                  </div>
                );
              }
              return (
                <div key={bubbleKey} className="max-w-3xl">
                  <p className="text-sm whitespace-pre-wrap">{display.text}</p>
                </div>
              );
            })}
            {chat.localMessages.map((local) => (
              <div key={local.id} className="ml-auto max-w-[85%] bg-muted px-3 py-2">
                <p className="text-sm whitespace-pre-wrap">{local.text}</p>
              </div>
            ))}
          </div>
          <form
            className="flex items-end gap-2 border-t border-border px-3 py-3"
            onSubmit={(event) => {
              event.preventDefault();
              void sendChatMessage();
            }}
          >
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              rows={1}
              aria-label="Message the DPIA agent"
              placeholder="Ask about a question before answering…"
              className="max-h-32 min-h-10 flex-1 resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              disabled={chat.streamState !== "connected" || chat.sending}
            />
            <Button
              type="submit"
              size="icon"
              aria-label="Send message"
              disabled={!message.trim() || chat.streamState !== "connected" || chat.sending}
            >
              {chat.sending ? <Loader2Icon className="animate-spin" /> : <SendIcon />}
            </Button>
          </form>
          {(chat.chatError ?? chat.streamState === "failed") && (
            <div className="flex items-center gap-2 border-t border-border px-4 py-2" role="alert">
              <AlertTriangleIcon className="size-4 shrink-0 text-status-red" />
              <span className="flex-1 text-sm text-status-red">{chat.chatError}</span>
              {chat.streamState === "failed" && (
                <Button type="button" variant="outline" size="sm" onClick={chat.retry}>
                  Retry connection
                </Button>
              )}
            </div>
          )}
        </section>

        <section
          aria-labelledby="respond-card-heading"
          className="rounded-lg border border-border bg-card px-4 py-4"
          data-testid="dpia-respond-card"
        >
          <h2 id="respond-card-heading" className="mb-1 text-ui font-semibold">
            {submitted ? "Your submitted answers" : "Scoped questions"}
          </h2>
          {!intro && (
            <p className="text-sm text-muted-foreground">
              Waiting for the shared questions to load from the conversation.
            </p>
          )}
          {intro && submitted && (
            <dl className="space-y-3 text-sm">
              {(submittedResponse?.answers ?? []).map((answer) => (
                <div key={answer.question_id}>
                  <dt className="font-medium">
                    {intro.questions.find(({ id }) => id === answer.question_id)?.text ??
                      answer.question_id}
                  </dt>
                  <dd className="text-muted-foreground">{answer.response}</dd>
                </div>
              ))}
              <p className="text-muted-foreground">
                The Privacy Officer reviews these before anything changes on the case.
              </p>
            </dl>
          )}
          {intro && !submitted && (
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-2">
                <div className="space-y-1">
                  <label htmlFor="respondent-name" className="text-sm font-medium">
                    Your name
                  </label>
                  <input
                    id="respondent-name"
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
                </div>
                <div className="space-y-1">
                  <label htmlFor="respondent-team" className="text-sm font-medium">
                    Your team
                  </label>
                  <input
                    id="respondent-team"
                    value={team}
                    onChange={(event) => setTeam(event.target.value)}
                    className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
                </div>
              </div>
              {intro.questions.map((question) => (
                <div key={question.id} className="space-y-1">
                  <label htmlFor={`answer-${question.id}`} className="text-sm font-medium">
                    {question.text}
                  </label>
                  <textarea
                    id={`answer-${question.id}`}
                    value={answers[question.id] ?? ""}
                    rows={3}
                    onChange={(event) =>
                      setAnswers((current) => ({ ...current, [question.id]: event.target.value }))
                    }
                    className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
                </div>
              ))}
              <Button
                type="button"
                className="w-full"
                disabled={!answersReady}
                onClick={() => setReviewOpen(true)}
              >
                Review &amp; submit answers
              </Button>
            </div>
          )}
        </section>
      </div>

      <Dialog open={reviewOpen} onOpenChange={setReviewOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirm your answers</DialogTitle>
            <DialogDescription>
              Sent to the Privacy Office as {name || "…"} ({team || "…"}). The officer reviews
              before the case changes.
            </DialogDescription>
          </DialogHeader>
          <dl className="space-y-2 text-sm">
            {(intro?.questions ?? []).map((question) => (
              <div key={question.id}>
                <dt className="font-medium">{question.text}</dt>
                <dd className="text-muted-foreground">{answers[question.id]}</dd>
              </div>
            ))}
          </dl>
          {submitError && (
            <p role="alert" className="text-sm text-status-red">
              {submitError}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setReviewOpen(false)}>
              Keep editing
            </Button>
            <Button type="button" disabled={submitting} onClick={() => void submitResponse()}>
              {submitting && <Loader2Icon className="animate-spin" data-icon="inline-start" />}
              Submit answers
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageScroll>
  );
}
