import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  ClipboardListIcon,
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
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import {
  buildDpiaRequestArtifact,
  newDpiaRequestId,
  parseDpiaOutcomeText,
  parseDpiaRequestText,
  type DpiaOutcome,
  type DpiaRequest,
  type DpiaRequestDraft,
} from "@/lib/dpia/requestArtifacts";
import {
  buildRequesterAgentMessage,
  createDpiaRequestSession,
  displayMessageFromWireText,
  DPIA_ACK_LABEL,
  DPIA_REQUEST_ID_LABEL,
  DPIA_REQUEST_STATUS_LABEL,
  listDpiaSessionsByRole,
  renameDpiaSession,
  setDpiaSessionLabels,
  type DpiaLabelledSession,
} from "@/lib/dpia/requestSession";
import { bubbleText, useDpiaSessionChat } from "./useDpiaSessionChat";

const EMPTY_DRAFT: DpiaRequestDraft = {
  requesterName: "",
  requesterTeam: "",
  title: "",
  purpose: "",
  dataSubjects: "",
  personalData: "",
  vendors: "",
  timeline: "",
  knownUnknowns: [],
};

const DECISION_LABELS: Record<DpiaOutcome["decision"], string> = {
  approved: "Approved",
  "approved-with-conditions": "Approved with conditions",
  rejected: "Rejected",
  "not-required": "DPIA not required",
};

export function DpiaRequestPage() {
  const [sessions, setSessions] = useState<DpiaLabelledSession[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [active, setActive] = useState<DpiaLabelledSession | null>(null);
  const [starting, setStarting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const agents = useAvailableAgents({ enabled: true });

  useEffect(() => {
    const abort = new AbortController();
    listDpiaSessionsByRole("requester", abort.signal)
      .then(setSessions)
      .catch((error: unknown) => {
        if (!abort.signal.aborted) {
          setListError(error instanceof Error ? error.message : "Could not list requests.");
        }
      });
    return () => abort.abort();
  }, []);

  async function startRequest() {
    const agent = agents.data?.find(({ name }) => name === "dpia-investigation");
    if (!agent) {
      setActionError(
        agents.isLoading
          ? "The DPIA agent catalogue is still loading."
          : "The registered dpia-investigation agent is unavailable.",
      );
      return;
    }
    setStarting(true);
    setActionError(null);
    try {
      const session = await createDpiaRequestSession(agent.id);
      setActive(session);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not start the request.");
    } finally {
      setStarting(false);
    }
  }

  return (
    <PageScroll
      maxWidthClassName="max-w-[1100px]"
      contentClassName="px-5 md:px-8"
      data-testid="dpia-request-page"
    >
      <header className="mb-6 border-b border-border pb-5">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium text-muted-foreground">
          <ShieldCheckIcon className="size-4" />
          Privacy operations
        </div>
        <h1 className="text-2xl font-semibold tracking-normal">Request a DPIA</h1>
        <p className="mt-1 max-w-2xl text-ui text-muted-foreground">
          Describe your project to the DPIA agent in the conversation or fill in the intake card.
          Both produce the same reviewed request for the Privacy Office.
        </p>
        <Badge variant="outline" className="mt-3">
          Synthetic data only
        </Badge>
      </header>

      {active === null ? (
        <section aria-labelledby="request-start-heading" className="space-y-4">
          <h2 id="request-start-heading" className="text-ui font-semibold">
            Your requests
          </h2>
          {listError && (
            <p role="alert" className="text-sm text-status-red">
              {listError}
            </p>
          )}
          {sessions === null && !listError && (
            <p className="text-sm text-muted-foreground">Loading existing requests…</p>
          )}
          {sessions !== null && sessions.length === 0 && (
            <p className="text-sm text-muted-foreground">No DPIA requests yet.</p>
          )}
          {sessions !== null && sessions.length > 0 && (
            <ul className="space-y-2">
              {sessions.map((session) => (
                <li
                  key={session.sessionId}
                  className="flex items-center justify-between gap-3 rounded-lg border border-border px-4 py-3"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {session.labels[DPIA_REQUEST_ID_LABEL] ?? "Draft request"}
                    </p>
                    <p className="text-sm text-muted-foreground">
                      Status: {session.labels[DPIA_REQUEST_STATUS_LABEL] ?? "draft"}
                    </p>
                  </div>
                  <Button type="button" variant="outline" onClick={() => setActive(session)}>
                    Open
                  </Button>
                </li>
              ))}
            </ul>
          )}
          <Button type="button" disabled={starting} onClick={() => void startRequest()}>
            {starting && <Loader2Icon className="animate-spin" data-icon="inline-start" />}
            Start a new request
          </Button>
          {actionError && (
            <p role="alert" className="text-sm text-status-red">
              {actionError}
            </p>
          )}
        </section>
      ) : (
        <ActiveRequest session={active} />
      )}
    </PageScroll>
  );
}

function ActiveRequest({ session }: { session: DpiaLabelledSession }) {
  const chat = useDpiaSessionChat(session.sessionId);
  const [draft, setDraft] = useState<DpiaRequestDraft>(EMPTY_DRAFT);
  const [message, setMessage] = useState("");
  const [reviewOpen, setReviewOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [localStatus, setLocalStatus] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(session.labels[DPIA_ACK_LABEL] === "true");

  const transcriptTexts = useMemo(
    () => [...chat.historyBubbles, ...chat.liveBubbles].map(bubbleText).filter(Boolean),
    [chat.historyBubbles, chat.liveBubbles],
  );
  const submittedRequest = useMemo(() => {
    for (let index = transcriptTexts.length - 1; index >= 0; index -= 1) {
      const parsed = parseDpiaRequestText(transcriptTexts[index]);
      if (parsed) return parsed;
    }
    return null;
  }, [transcriptTexts]);
  const outcome = useMemo(() => {
    for (let index = transcriptTexts.length - 1; index >= 0; index -= 1) {
      const parsed = parseDpiaOutcomeText(transcriptTexts[index]);
      if (parsed) return parsed;
    }
    return null;
  }, [transcriptTexts]);

  const status =
    localStatus ??
    (outcome
      ? outcome.decision === "not-required"
        ? "declined"
        : "completed"
      : submittedRequest
        ? "submitted"
        : (session.labels[DPIA_REQUEST_STATUS_LABEL] ?? "draft"));

  const reviewReady =
    draft.requesterName.trim().length >= 2 &&
    draft.requesterTeam.trim().length >= 2 &&
    draft.title.trim().length >= 3 &&
    draft.purpose.trim().length >= 10 &&
    draft.dataSubjects.trim().length >= 3 &&
    draft.personalData.trim().length >= 3 &&
    draft.vendors.trim().length >= 1 &&
    draft.timeline.trim().length >= 1;

  function buildArtifact(): DpiaRequest {
    const now = new Date();
    return buildDpiaRequestArtifact(draft, newDpiaRequestId(draft.title, now), now.toISOString());
  }

  async function submitRequest() {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const artifact = buildArtifact();
      const sent = await chat.sendMessage(
        JSON.stringify(artifact),
        `Submitted DPIA request: ${artifact.project.title}`,
      );
      if (!sent) throw new Error("The request message was not accepted.");
      await setDpiaSessionLabels(session.sessionId, {
        [DPIA_REQUEST_ID_LABEL]: artifact.request_id,
        [DPIA_REQUEST_STATUS_LABEL]: "submitted",
      });
      void renameDpiaSession(session.sessionId, `DPIA request: ${artifact.project.title}`).catch(
        () => undefined,
      );
      setLocalStatus("submitted");
      setReviewOpen(false);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "The request could not be sent.");
    } finally {
      setSubmitting(false);
    }
  }

  async function acknowledgeOutcome() {
    const sent = await chat.sendMessage(
      buildRequesterAgentMessage("Outcome acknowledged. Thank you."),
      "Outcome acknowledged. Thank you.",
    );
    if (sent) {
      await setDpiaSessionLabels(session.sessionId, { [DPIA_ACK_LABEL]: "true" });
      setAcknowledged(true);
    }
  }

  async function sendChatMessage() {
    const text = message.trim();
    if (!text) return;
    const sent = await chat.sendMessage(buildRequesterAgentMessage(text), text);
    if (sent) setMessage("");
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,26rem)]">
      <section
        aria-label="Request conversation"
        className="flex min-h-[24rem] flex-col rounded-lg border border-border bg-card"
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
          <p className="text-sm font-medium">DPIA agent conversation</p>
          <Badge variant="outline">{statusLabel(status)}</Badge>
        </div>
        <div
          className="flex-1 space-y-3 overflow-y-auto px-4 py-3"
          data-testid="dpia-request-transcript"
          aria-live="polite"
        >
          {transcriptTexts.length === 0 && chat.localMessages.length === 0 && (
            <p className="text-sm text-muted-foreground">
              Tell the agent about your project, or use the intake card. Example: “We want a vendor
              tool that scores student wellbeing surveys.”
            </p>
          )}
          {[...chat.historyBubbles, ...chat.liveBubbles].map((bubble) => {
            if (bubble.kind !== "user" && bubble.kind !== "assistant") return null;
            const bubbleKey =
              bubble.kind === "user" ? `u-${bubble.itemId}` : `a-${bubble.stableId}`;
            const raw = bubbleText(bubble);
            if (!raw) return null;
            if (parseDpiaRequestText(raw)) {
              return (
                <p
                  key={bubbleKey}
                  className="flex items-center gap-2 text-sm text-muted-foreground"
                >
                  <ClipboardListIcon className="size-4 shrink-0" />
                  Structured DPIA request submitted to the Privacy Office.
                </p>
              );
            }
            if (parseDpiaOutcomeText(raw)) return null;
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
            placeholder="Describe your project or ask a question…"
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

      <div className="space-y-4">
        {outcome && (
          <OutcomeCard
            outcome={outcome}
            acknowledged={acknowledged}
            onAcknowledge={() => void acknowledgeOutcome()}
          />
        )}
        {submittedRequest || status !== "draft" ? (
          <section
            aria-label="Request status"
            className="rounded-lg border border-border bg-card px-4 py-4"
            data-testid="dpia-request-status"
          >
            <h2 className="mb-1 text-ui font-semibold">Request status</h2>
            <p className="text-sm text-muted-foreground">
              {status === "submitted"
                ? "Submitted — awaiting Privacy Office triage. The officer may ask clarifying questions here."
                : statusLabel(status)}
            </p>
            {submittedRequest && (
              <dl className="mt-3 space-y-1 text-sm">
                <div className="flex justify-between gap-3">
                  <dt className="text-muted-foreground">Request</dt>
                  <dd className="truncate">{submittedRequest.request_id}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-muted-foreground">Project</dt>
                  <dd className="truncate">{submittedRequest.project.title}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-muted-foreground">Submitted</dt>
                  <dd>{submittedRequest.submitted_at.slice(0, 10)}</dd>
                </div>
              </dl>
            )}
          </section>
        ) : (
          <section
            aria-labelledby="intake-card-heading"
            className="rounded-lg border border-border bg-card px-4 py-4"
            data-testid="dpia-intake-card"
          >
            <h2 id="intake-card-heading" className="mb-1 text-ui font-semibold">
              Intake card
            </h2>
            <p className="mb-3 text-sm text-muted-foreground">
              The structured route to the same request the agent would draft with you.
            </p>
            <div className="space-y-3">
              <IntakeField
                label="Your name"
                value={draft.requesterName}
                onChange={(requesterName) => setDraft({ ...draft, requesterName })}
              />
              <IntakeField
                label="Your team"
                value={draft.requesterTeam}
                onChange={(requesterTeam) => setDraft({ ...draft, requesterTeam })}
              />
              <IntakeField
                label="Project title"
                value={draft.title}
                onChange={(title) => setDraft({ ...draft, title })}
              />
              <IntakeField
                label="Purpose"
                multiline
                value={draft.purpose}
                onChange={(purpose) => setDraft({ ...draft, purpose })}
              />
              <IntakeField
                label="Data subjects"
                value={draft.dataSubjects}
                onChange={(dataSubjects) => setDraft({ ...draft, dataSubjects })}
              />
              <IntakeField
                label="Personal data involved"
                multiline
                value={draft.personalData}
                onChange={(personalData) => setDraft({ ...draft, personalData })}
              />
              <IntakeField
                label="Vendors / processors"
                value={draft.vendors}
                onChange={(vendors) => setDraft({ ...draft, vendors })}
              />
              <IntakeField
                label="Timeline"
                value={draft.timeline}
                onChange={(timeline) => setDraft({ ...draft, timeline })}
              />
              <IntakeField
                label="Known unknowns (one per line)"
                multiline
                value={draft.knownUnknowns.join("\n")}
                onChange={(value) =>
                  setDraft({ ...draft, knownUnknowns: value.split("\n").map((s) => s.trim()) })
                }
              />
            </div>
            <Button
              type="button"
              className="mt-4 w-full"
              disabled={!reviewReady}
              onClick={() => setReviewOpen(true)}
            >
              Review &amp; submit
            </Button>
          </section>
        )}
      </div>

      <Dialog open={reviewOpen} onOpenChange={setReviewOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Review your DPIA request</DialogTitle>
            <DialogDescription>
              This is exactly what the Privacy Office receives. Submitting cannot be edited, only
              clarified in the conversation.
            </DialogDescription>
          </DialogHeader>
          <dl className="space-y-2 text-sm">
            {(
              [
                ["Requester", `${draft.requesterName} (${draft.requesterTeam})`],
                ["Project", draft.title],
                ["Purpose", draft.purpose],
                ["Data subjects", draft.dataSubjects],
                ["Personal data", draft.personalData],
                ["Vendors", draft.vendors],
                ["Timeline", draft.timeline],
                [
                  "Known unknowns",
                  draft.knownUnknowns.filter(Boolean).join("; ") || "None declared",
                ],
              ] as const
            ).map(([label, value]) => (
              <div key={label}>
                <dt className="font-medium">{label}</dt>
                <dd className="text-muted-foreground">{value}</dd>
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
            <Button type="button" disabled={submitting} onClick={() => void submitRequest()}>
              {submitting && <Loader2Icon className="animate-spin" data-icon="inline-start" />}
              Submit to DPIA Office
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function statusLabel(status: string): string {
  switch (status) {
    case "draft":
      return "Draft";
    case "submitted":
      return "Awaiting triage";
    case "accepted":
      return "In screening";
    case "declined":
      return "Closed — DPIA not required";
    case "completed":
      return "Outcome received";
    default:
      return status;
  }
}

function IntakeField({
  label,
  value,
  onChange,
  multiline = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  multiline?: boolean;
}) {
  const id = `intake-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {multiline ? (
        <textarea
          id={id}
          value={value}
          rows={2}
          onChange={(event) => onChange(event.target.value)}
          className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      ) : (
        <input
          id={id}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      )}
    </div>
  );
}

function OutcomeCard({
  outcome,
  acknowledged,
  onAcknowledge,
}: {
  outcome: DpiaOutcome;
  acknowledged: boolean;
  onAcknowledge: () => void;
}) {
  return (
    <section
      aria-label="DPIA outcome"
      className="rounded-lg border border-border bg-card px-4 py-4"
      data-testid="dpia-outcome-card"
    >
      <div className="mb-2 flex items-center justify-between gap-3">
        <h2 className="text-ui font-semibold">Privacy Office outcome</h2>
        <Badge variant="outline">{DECISION_LABELS[outcome.decision]}</Badge>
      </div>
      <div className="space-y-3 text-sm">
        <div>
          <p className="font-medium">Reasons</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-muted-foreground">
            {outcome.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
        {outcome.conditions.length > 0 && (
          <div>
            <p className="font-medium">Conditions</p>
            <ul className="mt-1 space-y-1 text-muted-foreground">
              {outcome.conditions.map((condition) => (
                <li key={condition.action}>
                  {condition.action} — {condition.owner}, due {condition.due}
                </li>
              ))}
            </ul>
          </div>
        )}
        <dl className="space-y-1">
          <div className="flex justify-between gap-3">
            <dt className="text-muted-foreground">Review date</dt>
            <dd>{outcome.review_date}</dd>
          </div>
          <div className="flex justify-between gap-3">
            <dt className="text-muted-foreground">Decided by</dt>
            <dd>{outcome.decided_by}</dd>
          </div>
          <div className="flex justify-between gap-3">
            <dt className="text-muted-foreground">Contact</dt>
            <dd className="truncate">{outcome.contact}</dd>
          </div>
        </dl>
      </div>
      <Button
        type="button"
        variant={acknowledged ? "outline" : "default"}
        className="mt-4 w-full"
        disabled={acknowledged}
        onClick={onAcknowledge}
      >
        <CheckCircle2Icon data-icon="inline-start" />
        {acknowledged ? "Acknowledged" : "Acknowledge outcome"}
      </Button>
    </section>
  );
}
