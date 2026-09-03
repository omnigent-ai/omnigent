import { useMemo, useState } from "react";
import {
  ArrowLeftIcon,
  CheckCircle2Icon,
  Loader2Icon,
  MessageCircleQuestionIcon,
  SendIcon,
  ShieldCheckIcon,
  XCircleIcon,
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
import { useDpiaRequests } from "@/hooks/useDpiaRequests";
import { recordDpiaCaseEvent, loadDpiaCase } from "@/lib/dpia/dpiaApi";
import {
  buildDpiaOutcomeArtifact,
  parseDpiaOutcomeText,
  parseDpiaRequestText,
  type DpiaOutcome,
} from "@/lib/dpia/requestArtifacts";
import {
  buildOfficerRelayMessage,
  displayMessageFromWireText,
  DPIA_CASE_ID_LABEL,
  DPIA_REQUEST_STATUS_LABEL,
  setDpiaSessionLabels,
} from "@/lib/dpia/requestSession";
import { postEvent } from "@/lib/sessionsApi";
import { Link, useParams } from "@/lib/routing";
import { bubbleText, useDpiaSessionChat } from "./useDpiaSessionChat";

const SCREENING_CASE_ID = "student-success-alert";
const OFFICER = "Alex Morgan";

export function DpiaRequestReviewPage() {
  const { requestId = "" } = useParams<{ requestId: string }>();
  const requests = useDpiaRequests();
  const summary = requests.data?.find((candidate) => candidate.requestId === requestId);

  return (
    <PageScroll
      maxWidthClassName="max-w-[1100px]"
      contentClassName="px-5 md:px-8"
      data-testid="dpia-request-review-page"
    >
      <header className="mb-6 border-b border-border pb-5">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium text-muted-foreground">
          <ShieldCheckIcon className="size-4" />
          Privacy operations
        </div>
        <h1 className="text-2xl font-semibold tracking-normal">DPIA request review</h1>
        <p className="mt-1 text-ui text-muted-foreground">
          Triage the incoming request, ask clarifications, and publish the approved outcome.
        </p>
        <Button asChild variant="ghost" size="sm" className="mt-2 -ml-2">
          <Link to="/dpia">
            <ArrowLeftIcon data-icon="inline-start" />
            Back to DPIA desk
          </Link>
        </Button>
      </header>
      {requests.isLoading && <p className="text-sm text-muted-foreground">Loading request…</p>}
      {requests.isError && (
        <p role="alert" className="text-sm text-status-red">
          Could not load DPIA requests from the server.
        </p>
      )}
      {!requests.isLoading && !requests.isError && !summary && (
        <p className="text-sm text-muted-foreground">No request found with id {requestId}.</p>
      )}
      {summary && <RequestReview key={summary.sessionId} summary={summary} />}
    </PageScroll>
  );
}

interface Summary {
  sessionId: string;
  requestId: string | null;
  status: string;
  acknowledged: boolean;
  caseId: string | null;
  request: ReturnType<typeof parseDpiaRequestText>;
  outcome: DpiaOutcome | null;
}

function RequestReview({ summary }: { summary: Summary }) {
  const chat = useDpiaSessionChat(summary.sessionId);
  const [status, setStatus] = useState(summary.status);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [clarification, setClarification] = useState("");
  const [declineOpen, setDeclineOpen] = useState(false);
  const [outcomeOpen, setOutcomeOpen] = useState(false);
  const request = summary.request;

  const transcript = useMemo(
    () =>
      [...chat.historyBubbles, ...chat.liveBubbles].flatMap((bubble) => {
        if (bubble.kind !== "user" && bubble.kind !== "assistant") return [];
        const raw = bubbleText(bubble);
        if (!raw || parseDpiaRequestText(raw) || parseDpiaOutcomeText(raw)) return [];
        const display = displayMessageFromWireText(raw);
        return [
          {
            key: bubble.kind === "user" ? `u-${bubble.itemId}` : `a-${bubble.stableId}`,
            sender: bubble.kind === "assistant" ? ("agent" as const) : display.sender,
            text: display.text,
          },
        ];
      }),
    [chat.historyBubbles, chat.liveBubbles],
  );

  async function runAction(name: string, action: () => Promise<void>) {
    setBusy(name);
    setActionError(null);
    try {
      await action();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : `The ${name} action failed.`);
    } finally {
      setBusy(null);
    }
  }

  function acceptForScreening() {
    return runAction("accept", async () => {
      await setDpiaSessionLabels(summary.sessionId, {
        [DPIA_REQUEST_STATUS_LABEL]: "accepted",
        [DPIA_CASE_ID_LABEL]: SCREENING_CASE_ID,
      });
      recordDpiaCaseEvent(SCREENING_CASE_ID, {
        actor: OFFICER,
        action: "Accepted DPIA request for screening",
        object: request?.project.title ?? summary.requestId ?? "DPIA request",
        timestamp: new Date().toISOString(),
        newValue: summary.requestId ?? undefined,
      });
      setStatus("accepted");
    });
  }

  function sendClarification() {
    const text = clarification.trim();
    if (!text) return Promise.resolve();
    return runAction("clarify", async () => {
      const result = await postEvent(summary.sessionId, {
        type: "message",
        data: {
          role: "user",
          content: [{ type: "input_text", text: buildOfficerRelayMessage(text) }],
        },
      });
      if (result.denied) throw new Error("The agent policy declined the clarification.");
      setClarification("");
    });
  }

  function publishOutcome(outcome: DpiaOutcome, nextStatus: "declined" | "completed") {
    return runAction("outcome", async () => {
      const result = await postEvent(summary.sessionId, {
        type: "message",
        data: { role: "user", content: [{ type: "input_text", text: JSON.stringify(outcome) }] },
      });
      if (result.denied) throw new Error("The agent policy declined the outcome message.");
      await setDpiaSessionLabels(summary.sessionId, {
        [DPIA_REQUEST_STATUS_LABEL]: nextStatus,
      });
      recordDpiaCaseEvent(SCREENING_CASE_ID, {
        actor: OFFICER,
        action:
          nextStatus === "declined"
            ? "Declined DPIA request at triage"
            : "Sent DPIA outcome to requester",
        object: request?.project.title ?? outcome.request_id,
        timestamp: outcome.decided_at,
        newValue: outcome.decision,
      });
      setStatus(nextStatus);
      setDeclineOpen(false);
      setOutcomeOpen(false);
    });
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
      <div className="space-y-4">
        <section
          aria-label="Request details"
          className="rounded-lg border border-border bg-card px-4 py-4"
          data-testid="dpia-request-detail-card"
        >
          <div className="mb-2 flex items-center justify-between gap-3">
            <h2 className="text-ui font-semibold">Incoming request</h2>
            <Badge variant="outline">{status}</Badge>
          </div>
          {!request && (
            <p className="text-sm text-muted-foreground">
              The structured request artifact has not arrived in this session yet.
            </p>
          )}
          {request && (
            <dl className="space-y-2 text-sm">
              {(
                [
                  ["Request", request.request_id],
                  ["Requester", `${request.requester.name} (${request.requester.team})`],
                  ["Project", request.project.title],
                  ["Purpose", request.project.purpose],
                  ["Data subjects", request.project.data_subjects],
                  ["Personal data", request.project.personal_data],
                  ["Vendors", request.project.vendors],
                  ["Timeline", request.project.timeline],
                  [
                    "Known unknowns",
                    request.known_unknowns.length > 0
                      ? request.known_unknowns.join("; ")
                      : "None declared",
                  ],
                  ["Submitted", request.submitted_at.slice(0, 10)],
                ] as const
              ).map(([label, value]) => (
                <div key={label}>
                  <dt className="font-medium">{label}</dt>
                  <dd className="break-words text-muted-foreground">{value}</dd>
                </div>
              ))}
            </dl>
          )}
        </section>

        <section
          aria-label="Triage actions"
          className="space-y-3 rounded-lg border border-border bg-card px-4 py-4"
        >
          <h2 className="text-ui font-semibold">Officer actions</h2>
          {status === "submitted" && (
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                disabled={busy !== null || !request}
                onClick={() => void acceptForScreening()}
              >
                {busy === "accept" && (
                  <Loader2Icon className="animate-spin" data-icon="inline-start" />
                )}
                <CheckCircle2Icon data-icon="inline-start" />
                Accept for screening
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={busy !== null}
                onClick={() => setDeclineOpen(true)}
              >
                <XCircleIcon data-icon="inline-start" />
                Decline — DPIA not required
              </Button>
            </div>
          )}
          {status === "accepted" && (
            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">
                Attached to the screening case. Review the evidence, record the officer decision,
                then send the outcome to the requester.
              </p>
              <div className="flex flex-wrap gap-2">
                <Button asChild variant="outline">
                  <Link to={`/dpia/cases/${SCREENING_CASE_ID}`}>Open screening case</Link>
                </Button>
                <Button type="button" disabled={busy !== null} onClick={() => setOutcomeOpen(true)}>
                  <SendIcon data-icon="inline-start" />
                  Send outcome to requester
                </Button>
              </div>
            </div>
          )}
          {(status === "declined" || status === "completed") && (
            <p className="text-sm text-muted-foreground">
              Outcome sent{summary.acknowledged ? " and acknowledged by the requester." : "."}
            </p>
          )}
          <div className="space-y-2 border-t border-border pt-3">
            <label htmlFor="clarification" className="flex items-center gap-2 text-sm font-medium">
              <MessageCircleQuestionIcon className="size-4" />
              Ask the requester for clarification
            </label>
            <textarea
              id="clarification"
              value={clarification}
              rows={2}
              onChange={(event) => setClarification(event.target.value)}
              placeholder="This appears in the requester's conversation as a Privacy Office message."
              className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!clarification.trim() || busy !== null}
              onClick={() => void sendClarification()}
            >
              {busy === "clarify" && (
                <Loader2Icon className="animate-spin" data-icon="inline-start" />
              )}
              Send clarification
            </Button>
          </div>
          {actionError && (
            <p role="alert" className="text-sm text-status-red">
              {actionError}
            </p>
          )}
        </section>
      </div>

      <section
        aria-label="Requester conversation"
        className="rounded-lg border border-border bg-card"
      >
        <div className="border-b border-border px-4 py-2.5">
          <p className="text-sm font-medium">Requester conversation</p>
        </div>
        <div className="max-h-[38rem] space-y-3 overflow-y-auto px-4 py-3">
          {transcript.length === 0 && (
            <p className="text-sm text-muted-foreground">No conversation yet.</p>
          )}
          {transcript.map((entry) => (
            <div
              key={entry.key}
              className={
                entry.sender === "officer"
                  ? "ml-auto max-w-[85%] rounded-md bg-muted px-3 py-2"
                  : "max-w-[85%] rounded-md border border-border px-3 py-2"
              }
            >
              <p className="mb-1 text-xs font-medium text-muted-foreground">
                {entry.sender === "officer"
                  ? "Privacy Office"
                  : entry.sender === "agent"
                    ? "DPIA agent"
                    : "Requester"}
              </p>
              <p className="text-sm whitespace-pre-wrap">{entry.text}</p>
            </div>
          ))}
        </div>
      </section>

      <DeclineDialog
        open={declineOpen}
        busy={busy === "outcome"}
        requestId={summary.requestId ?? request?.request_id ?? ""}
        onOpenChange={setDeclineOpen}
        onDecline={(outcome) => void publishOutcome(outcome, "declined")}
      />
      <OutcomeDialog
        open={outcomeOpen}
        busy={busy === "outcome"}
        requestId={summary.requestId ?? request?.request_id ?? ""}
        onOpenChange={setOutcomeOpen}
        onSend={(outcome) => void publishOutcome(outcome, "completed")}
      />
    </div>
  );
}

function DeclineDialog({
  open,
  busy,
  requestId,
  onOpenChange,
  onDecline,
}: {
  open: boolean;
  busy: boolean;
  requestId: string;
  onOpenChange: (open: boolean) => void;
  onDecline: (outcome: DpiaOutcome) => void;
}) {
  const [reason, setReason] = useState("");
  const ready = reason.trim().length >= 10;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Decline — DPIA not required</DialogTitle>
          <DialogDescription>
            The requester receives this reasoning as the recorded outcome.
          </DialogDescription>
        </DialogHeader>
        <label htmlFor="decline-reason" className="text-sm font-medium">
          Reason (at least 10 characters)
        </label>
        <textarea
          id="decline-reason"
          value={reason}
          rows={3}
          onChange={(event) => setReason(event.target.value)}
          className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            type="button"
            disabled={!ready || busy}
            onClick={() =>
              onDecline(
                buildDpiaOutcomeArtifact(
                  {
                    requestId,
                    decision: "not-required",
                    reasons: [reason.trim()],
                    conditions: [],
                    reviewDate: "none",
                    contact: "privacy-office@university.example",
                    decidedBy: OFFICER,
                  },
                  new Date().toISOString(),
                ),
              )
            }
          >
            {busy && <Loader2Icon className="animate-spin" data-icon="inline-start" />}
            Record decline
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function OutcomeDialog({
  open,
  busy,
  requestId,
  onOpenChange,
  onSend,
}: {
  open: boolean;
  busy: boolean;
  requestId: string;
  onOpenChange: (open: boolean) => void;
  onSend: (outcome: DpiaOutcome) => void;
}) {
  const officerDecision = loadDpiaCase(SCREENING_CASE_ID).caseData.officerDecision;
  const [decision, setDecision] = useState<DpiaOutcome["decision"]>("approved-with-conditions");
  const [reasons, setReasons] = useState(officerDecision?.rationale ?? "");
  const [conditionAction, setConditionAction] = useState(
    "Resolve the outstanding evidence gaps identified in screening.",
  );
  const [conditionOwner, setConditionOwner] = useState("Requester");
  const [conditionDue, setConditionDue] = useState("2026-10-01");
  const [reviewDate, setReviewDate] = useState("2027-02-01");
  const needsCondition = decision === "approved-with-conditions";
  const ready =
    reasons.trim().length >= 5 &&
    (!needsCondition ||
      (conditionAction.trim().length >= 5 &&
        conditionOwner.trim().length >= 2 &&
        conditionDue.trim().length >= 4));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Send outcome to requester</DialogTitle>
          <DialogDescription>
            {officerDecision
              ? "Drafted from the recorded officer decision. Only this requester-safe summary is shared."
              : "No officer decision is recorded on the screening case yet — record one first, or send a provisional outcome."}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-sm">
          <div className="space-y-1">
            <label htmlFor="outcome-decision" className="font-medium">
              Decision
            </label>
            <select
              id="outcome-decision"
              value={decision}
              onChange={(event) => setDecision(event.target.value as DpiaOutcome["decision"])}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
            >
              <option value="approved">Approved</option>
              <option value="approved-with-conditions">Approved with conditions</option>
              <option value="rejected">Rejected</option>
            </select>
          </div>
          <div className="space-y-1">
            <label htmlFor="outcome-reasons" className="font-medium">
              Reasons (one per line)
            </label>
            <textarea
              id="outcome-reasons"
              value={reasons}
              rows={3}
              onChange={(event) => setReasons(event.target.value)}
              className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
          {needsCondition && (
            <div className="space-y-2 rounded-md border border-border px-3 py-2">
              <p className="font-medium">Condition</p>
              <input
                aria-label="Condition action"
                value={conditionAction}
                onChange={(event) => setConditionAction(event.target.value)}
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              />
              <div className="flex gap-2">
                <input
                  aria-label="Condition owner"
                  value={conditionOwner}
                  placeholder="Owner"
                  onChange={(event) => setConditionOwner(event.target.value)}
                  className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                />
                <input
                  aria-label="Condition due date"
                  value={conditionDue}
                  placeholder="Due"
                  onChange={(event) => setConditionDue(event.target.value)}
                  className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                />
              </div>
            </div>
          )}
          <div className="space-y-1">
            <label htmlFor="outcome-review" className="font-medium">
              Review date
            </label>
            <input
              id="outcome-review"
              value={reviewDate}
              onChange={(event) => setReviewDate(event.target.value)}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
            />
          </div>
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            type="button"
            disabled={!ready || busy}
            onClick={() =>
              onSend(
                buildDpiaOutcomeArtifact(
                  {
                    requestId,
                    caseId: SCREENING_CASE_ID,
                    decision,
                    reasons: reasons
                      .split("\n")
                      .map((line) => line.trim())
                      .filter(Boolean),
                    conditions: needsCondition
                      ? [
                          {
                            action: conditionAction,
                            owner: conditionOwner,
                            due: conditionDue,
                          },
                        ]
                      : [],
                    reviewDate,
                    contact: "privacy-office@university.example",
                    decidedBy: OFFICER,
                  },
                  new Date().toISOString(),
                ),
              )
            }
          >
            {busy && <Loader2Icon className="animate-spin" data-icon="inline-start" />}
            Send outcome
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
