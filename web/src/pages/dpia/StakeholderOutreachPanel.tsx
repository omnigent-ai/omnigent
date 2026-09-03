import { useState } from "react";
import {
  CheckCircle2Icon,
  ExternalLinkIcon,
  Loader2Icon,
  Share2Icon,
  UsersIcon,
  XCircleIcon,
} from "lucide-react";
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
import { useDpiaContributorResponses } from "@/hooks/useDpiaRequests";
import { recordDpiaCaseEvent } from "@/lib/dpia/dpiaApi";
import type { DpiaContributorSummary } from "@/lib/dpia/requestInbox";
import {
  buildContributorIntroMessage,
  buildOfficerRelayMessage,
  createDpiaContributorSession,
  DPIA_RESPONSE_STATUS_LABEL,
  setDpiaSessionLabels,
} from "@/lib/dpia/requestSession";
import type { DpiaCaseSnapshot } from "@/lib/dpia/types";
import { postEvent } from "@/lib/sessionsApi";
import { Link } from "@/lib/routing";

const OFFICER = "Alex Morgan";

interface StakeholderOutreachPanelProps {
  caseData: DpiaCaseSnapshot;
  onAcceptAnswer: (input: { questionId: string; response: string; answeredBy: string }) => void;
}

export function StakeholderOutreachPanel({
  caseData,
  onAcceptAnswer,
}: StakeholderOutreachPanelProps) {
  const responses = useDpiaContributorResponses(caseData.id);
  const agents = useAvailableAgents({ enabled: true });
  const [shareOpen, setShareOpen] = useState(false);
  const [busySessionId, setBusySessionId] = useState<string | null>(null);
  const [panelError, setPanelError] = useState<string | null>(null);

  const openQuestions = caseData.questions.filter(({ status }) => status !== "answered");
  const rows = responses.data ?? [];
  const pending = rows.filter((row) => row.status === "submitted" && row.response !== null);

  async function acceptResponse(row: DpiaContributorSummary) {
    if (!row.response) return;
    setBusySessionId(row.sessionId);
    setPanelError(null);
    try {
      const knownQuestionIds = new Set(caseData.questions.map(({ id }) => id));
      const unknown = row.response.answers.filter(
        ({ question_id }) => !knownQuestionIds.has(question_id),
      );
      if (unknown.length > 0) {
        throw new Error(
          `The response references unknown questions: ${unknown
            .map(({ question_id }) => question_id)
            .join(", ")}.`,
        );
      }
      const answeredBy = `${row.response.respondent.name} (${row.response.respondent.team})`;
      for (const answer of row.response.answers) {
        onAcceptAnswer({
          questionId: answer.question_id,
          response: answer.response,
          answeredBy,
        });
      }
      await setDpiaSessionLabels(row.sessionId, { [DPIA_RESPONSE_STATUS_LABEL]: "accepted" });
      await postEvent(row.sessionId, {
        type: "message",
        data: {
          role: "user",
          content: [
            {
              type: "input_text",
              text: buildOfficerRelayMessage(
                "Your answers were accepted and recorded on the case. Thank you.",
              ),
            },
          ],
        },
      });
      await responses.refetch();
    } catch (error) {
      setPanelError(error instanceof Error ? error.message : "The response could not be accepted.");
    } finally {
      setBusySessionId(null);
    }
  }

  async function rejectResponse(row: DpiaContributorSummary) {
    setBusySessionId(row.sessionId);
    setPanelError(null);
    try {
      await setDpiaSessionLabels(row.sessionId, { [DPIA_RESPONSE_STATUS_LABEL]: "rejected" });
      await postEvent(row.sessionId, {
        type: "message",
        data: {
          role: "user",
          content: [
            {
              type: "input_text",
              text: buildOfficerRelayMessage(
                "The Privacy Officer could not accept these answers as evidence. Please review and resubmit.",
              ),
            },
          ],
        },
      });
      await responses.refetch();
    } catch (error) {
      setPanelError(error instanceof Error ? error.message : "The response could not be rejected.");
    } finally {
      setBusySessionId(null);
    }
  }

  return (
    <section
      aria-labelledby="stakeholder-outreach-heading"
      className="dpia-no-print mt-6 rounded-lg border border-border bg-card"
      data-testid="stakeholder-outreach-panel"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <UsersIcon className="size-4 text-muted-foreground" />
          <h2 id="stakeholder-outreach-heading" className="text-ui font-semibold">
            Stakeholder outreach
          </h2>
          {pending.length > 0 && <Badge>{pending.length} awaiting review</Badge>}
        </div>
        <Button
          type="button"
          variant="outline"
          disabled={openQuestions.length === 0}
          onClick={() => setShareOpen(true)}
        >
          <Share2Icon data-icon="inline-start" />
          Share questions with a stakeholder
        </Button>
      </div>
      <div className="space-y-3 px-4 py-3">
        {rows.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No stakeholder conversations yet. Share open questions to collect scoped answers
            in-product instead of transcribing emails.
          </p>
        )}
        {rows.map((row) => (
          <article
            key={row.sessionId}
            className="rounded-md border border-border px-3 py-3"
            data-testid="stakeholder-outreach-row"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <p className="text-sm font-medium">{row.contributor}</p>
                <Badge variant="outline">{row.status}</Badge>
              </div>
              <Button asChild variant="ghost" size="sm">
                <Link to={`/dpia/respond/${row.sessionId}`}>
                  <ExternalLinkIcon data-icon="inline-start" />
                  Open stakeholder view
                </Link>
              </Button>
            </div>
            {row.status === "submitted" && row.response && (
              <div className="mt-2 space-y-2">
                <dl className="space-y-2 text-sm">
                  {row.response.answers.map((answer) => (
                    <div key={answer.question_id}>
                      <dt className="font-medium">
                        {caseData.questions.find(({ id }) => id === answer.question_id)?.text ??
                          answer.question_id}
                      </dt>
                      <dd className="text-muted-foreground">{answer.response}</dd>
                    </div>
                  ))}
                </dl>
                <p className="text-sm text-muted-foreground">
                  Answered by {row.response.respondent.name} ({row.response.respondent.team})
                </p>
                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    size="sm"
                    disabled={busySessionId !== null}
                    onClick={() => void acceptResponse(row)}
                  >
                    {busySessionId === row.sessionId && (
                      <Loader2Icon className="animate-spin" data-icon="inline-start" />
                    )}
                    <CheckCircle2Icon data-icon="inline-start" />
                    Accept as recorded answers
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={busySessionId !== null}
                    onClick={() => void rejectResponse(row)}
                  >
                    <XCircleIcon data-icon="inline-start" />
                    Reject
                  </Button>
                </div>
              </div>
            )}
          </article>
        ))}
        {panelError && (
          <p role="alert" className="text-sm text-status-red">
            {panelError}
          </p>
        )}
      </div>

      <ShareDialog
        open={shareOpen}
        caseData={caseData}
        openQuestions={openQuestions}
        agentId={agents.data?.find(({ name }) => name === "dpia-investigation")?.id ?? null}
        onOpenChange={setShareOpen}
        onShared={() => void responses.refetch()}
      />
    </section>
  );
}

function ShareDialog({
  open,
  caseData,
  openQuestions,
  agentId,
  onOpenChange,
  onShared,
}: {
  open: boolean;
  caseData: DpiaCaseSnapshot;
  openQuestions: DpiaCaseSnapshot["questions"];
  agentId: string | null;
  onOpenChange: (open: boolean) => void;
  onShared: () => void;
}) {
  const [contributor, setContributor] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [sharing, setSharing] = useState(false);
  const [shareError, setShareError] = useState<string | null>(null);
  const [sharedLink, setSharedLink] = useState<string | null>(null);

  function toggle(questionId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(questionId)) next.delete(questionId);
      else next.add(questionId);
      return next;
    });
  }

  async function share() {
    if (!agentId) {
      setShareError("The registered dpia-investigation agent is unavailable.");
      return;
    }
    setSharing(true);
    setShareError(null);
    try {
      const questions = openQuestions
        .filter(({ id }) => selected.has(id))
        .map(({ id, text }) => ({ id, text }));
      const session = await createDpiaContributorSession(agentId, {
        caseId: caseData.id,
        contributor: contributor.trim(),
      });
      const intro = buildContributorIntroMessage({
        caseId: caseData.id,
        caseTitle: caseData.title,
        contributor: contributor.trim(),
        questions,
      });
      const result = await postEvent(session.sessionId, {
        type: "message",
        data: { role: "user", content: [{ type: "input_text", text: intro }] },
      });
      if (result.denied) throw new Error("The agent policy declined the outreach message.");
      recordDpiaCaseEvent(caseData.id, {
        actor: OFFICER,
        action: "Shared scoped questions with stakeholder",
        object: contributor.trim(),
        timestamp: new Date().toISOString(),
        newValue: questions.map(({ id }) => id).join(", "),
      });
      setSharedLink(`/dpia/respond/${session.sessionId}`);
      onShared();
    } catch (error) {
      setShareError(error instanceof Error ? error.message : "The outreach could not be created.");
    } finally {
      setSharing(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setSharedLink(null);
          setSelected(new Set());
          setContributor("");
          setShareError(null);
        }
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Share questions with a stakeholder</DialogTitle>
          <DialogDescription>
            The stakeholder gets a scoped conversation with only these questions — never the full
            case, other stakeholders&apos; answers, or officer deliberation.
          </DialogDescription>
        </DialogHeader>
        {sharedLink ? (
          <div className="space-y-3">
            <p className="text-sm">
              Outreach created. Send the stakeholder this link to answer in-product:
            </p>
            <p className="rounded-md border border-border bg-muted px-3 py-2 text-sm break-all">
              {sharedLink}
            </p>
            <DialogFooter>
              <Button asChild variant="outline">
                <Link to={sharedLink}>Open stakeholder view</Link>
              </Button>
              <Button type="button" onClick={() => onOpenChange(false)}>
                Done
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <>
            <div className="space-y-3">
              <div className="space-y-1">
                <label htmlFor="share-contributor" className="text-sm font-medium">
                  Stakeholder team
                </label>
                <input
                  id="share-contributor"
                  value={contributor}
                  placeholder="e.g. IT Security"
                  onChange={(event) => setContributor(event.target.value)}
                  className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
              </div>
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium">Open questions to share</legend>
                {openQuestions.map((question) => (
                  <label key={question.id} className="flex items-start gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={selected.has(question.id)}
                      onChange={() => toggle(question.id)}
                      className="mt-1"
                    />
                    <span>
                      <span className="font-medium">{question.stakeholder}:</span> {question.text}
                    </span>
                  </label>
                ))}
              </fieldset>
              {shareError && (
                <p role="alert" className="text-sm text-status-red">
                  {shareError}
                </p>
              )}
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button
                type="button"
                disabled={contributor.trim().length < 2 || selected.size === 0 || sharing}
                onClick={() => void share()}
              >
                {sharing && <Loader2Icon className="animate-spin" data-icon="inline-start" />}
                Create scoped outreach
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
