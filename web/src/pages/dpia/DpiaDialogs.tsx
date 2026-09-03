import { type FormEvent, useEffect, useMemo, useState } from "react";
import {
  AlertTriangleIcon,
  ArrowUpRightIcon,
  BotIcon,
  CheckCircle2Icon,
  ExternalLinkIcon,
  FileTextIcon,
  Loader2Icon,
  RotateCcwIcon,
  ScaleIcon,
  ShieldCheckIcon,
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
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  AgentActivity,
  Determination,
  DpiaCaseSnapshot,
  EvidenceItem,
  OfficerDecision,
  StakeholderQuestion,
} from "@/lib/dpia/types";
import { Link } from "@/lib/routing";
import { DpiaStatus } from "./DpiaStatus";

function submissionError(error: unknown): string {
  return error instanceof Error ? error.message : "The DPIA case update could not be saved.";
}

export function DpiaIntakeDialog({
  open,
  onOpenChange,
  caseData,
  onSave,
  busy = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  caseData: DpiaCaseSnapshot;
  onSave: (values: Record<string, string>) => Promise<unknown>;
  busy?: boolean;
}) {
  const initialValues = useMemo(
    () => Object.fromEntries(caseData.processingModel.facts.map((fact) => [fact.id, fact.value])),
    [caseData.processingModel.facts],
  );
  const [values, setValues] = useState<Record<string, string>>(initialValues);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = busy || submitting;
  useEffect(() => {
    if (open) setValues(initialValues);
  }, [initialValues, open]);
  useEffect(() => {
    if (!open) setError(null);
  }, [open]);
  const sections = useMemo(() => {
    const grouped = new Map<string, typeof caseData.processingModel.facts>();
    for (const fact of caseData.processingModel.facts) {
      grouped.set(fact.section, [...(grouped.get(fact.section) ?? []), fact]);
    }
    return Array.from(grouped.entries());
  }, [caseData.processingModel.facts]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSave(values);
      onOpenChange(false);
    } catch (cause) {
      setError(submissionError(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !pending && onOpenChange(next)}>
      <DialogContent className="max-h-[88vh] max-w-[min(960px,calc(100%-2rem))] grid-rows-[auto_minmax(0,1fr)_auto] p-0">
        <DialogHeader className="border-b border-border px-6 pt-6 pb-4">
          <DialogTitle>Edit processing intake</DialogTitle>
          <DialogDescription>
            Saving a material change creates one new processing-model version and marks dependent
            conclusions stale. The prior values remain in the audit trail.
          </DialogDescription>
        </DialogHeader>
        <form id="dpia-intake-form" onSubmit={submit} className="min-h-0 overflow-y-auto px-6 py-5">
          <div className="space-y-7">
            {sections.map(([section, facts]) => (
              <fieldset key={section} className="grid gap-3 md:grid-cols-2">
                <legend className="mb-2 w-full border-b border-border pb-2 text-ui font-semibold md:col-span-2">
                  {section}
                </legend>
                {facts.map((fact) => (
                  <label
                    key={fact.id}
                    htmlFor={`dpia-fact-${fact.id}`}
                    className={facts.length === 1 ? "md:col-span-2" : undefined}
                  >
                    <span className="mb-1.5 flex items-center gap-2 text-sm font-medium">
                      {fact.label}
                      {fact.material && <span className="text-muted-foreground">Material</span>}
                    </span>
                    <Textarea
                      id={`dpia-fact-${fact.id}`}
                      aria-label={fact.label}
                      value={values[fact.id] ?? ""}
                      onChange={(event) =>
                        setValues((current) => ({ ...current, [fact.id]: event.target.value }))
                      }
                      className="min-h-20"
                    />
                  </label>
                ))}
              </fieldset>
            ))}
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
          </div>
        </form>
        <DialogFooter className="m-0 rounded-none border-t px-6 py-4">
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <Button type="submit" form="dpia-intake-form" disabled={pending}>
            {pending && <Loader2Icon className="animate-spin" />}
            {pending ? "Saving" : "Save new version"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function EvidenceDialog({
  evidence,
  caseData,
  onOpenChange,
}: {
  evidence: EvidenceItem | null;
  caseData: DpiaCaseSnapshot;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={evidence !== null} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        {evidence && (
          <>
            <DialogHeader>
              <div className="mb-1 flex items-center gap-2">
                <Badge variant="outline">Synthetic evidence</Badge>
                <DpiaStatus status={evidence.status} />
              </div>
              <DialogTitle>{evidence.title}</DialogTitle>
              <DialogDescription>
                {evidence.source} · {evidence.owner} · Collected {formatDate(evidence.collectedAt)}
              </DialogDescription>
            </DialogHeader>
            <div className="grid gap-5">
              <section>
                <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
                  Relevant excerpt
                </h3>
                <blockquote className="rounded-lg border border-border bg-muted/30 px-4 py-3 text-ui leading-relaxed">
                  “{evidence.excerpt}”
                </blockquote>
              </section>
              <section>
                <h3 className="mb-2 text-sm font-semibold text-muted-foreground">Supports</h3>
                <div className="flex flex-wrap gap-2">
                  {evidence.supportedDimensionIds.map((dimensionId) => (
                    <Badge key={dimensionId} variant="secondary">
                      {caseData.determinations.find(({ dimensionId: id }) => id === dimensionId)
                        ?.question ?? dimensionId}
                    </Badge>
                  ))}
                </div>
              </section>
              <div className="grid grid-cols-2 gap-4 border-t border-border pt-4 text-sm">
                <div>
                  <span className="block text-muted-foreground">Artifact</span>
                  <span className="font-mono">{evidence.filename}</span>
                </div>
                <div>
                  <span className="block text-muted-foreground">Evidence ID</span>
                  <span className="font-mono">{evidence.id}</span>
                </div>
              </div>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function FindingProvenanceDialog({
  finding,
  caseData,
  onEvidence,
  onOpenChange,
}: {
  finding: Determination | null;
  caseData: DpiaCaseSnapshot;
  onEvidence: (evidence: EvidenceItem) => void;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={finding !== null} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[88vh] max-w-3xl overflow-y-auto">
        {finding && (
          <>
            <DialogHeader>
              <div className="mb-1 flex items-center gap-2">
                <DpiaStatus status={finding.status} />
                <Badge variant="outline">Model v{finding.processingModelVersion}</Badge>
              </div>
              <DialogTitle>{finding.question}</DialogTitle>
              <DialogDescription>
                Proposed by {finding.reviewer}. This is specialist analysis for Privacy Officer
                verification, not a legal determination.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-6">
              <section>
                <h3 className="mb-2 flex items-center gap-2 font-semibold">
                  <ScaleIcon className="size-4 text-muted-foreground" />
                  Professional reasoning
                </h3>
                <p className="leading-relaxed">{finding.reasoning}</p>
                {finding.dissent && (
                  <div className="mt-3 rounded-lg border border-border bg-muted/30 px-4 py-3">
                    <p className="text-sm font-semibold">Preserved disagreement</p>
                    <p className="mt-1 text-ui text-muted-foreground">{finding.dissent}</p>
                  </div>
                )}
              </section>
              <section>
                <h3 className="mb-2 font-semibold">Evidence trail</h3>
                <div className="grid gap-2">
                  {finding.evidenceReferences.map((reference) => {
                    const item = caseData.evidence.find(({ id }) => id === reference.evidenceId);
                    return (
                      <button
                        key={`${finding.id}-${reference.evidenceId}`}
                        type="button"
                        onClick={() => item && onEvidence(item)}
                        className="flex items-start justify-between gap-3 rounded-md border border-border px-3 py-2 text-left transition-colors hover:bg-muted"
                      >
                        <span>
                          <span className="block font-medium">
                            {item?.title ?? reference.evidenceId}
                          </span>
                          <span className="mt-0.5 block text-sm text-muted-foreground">
                            “{reference.excerpt}”
                          </span>
                        </span>
                        <ArrowUpRightIcon className="mt-1 size-4 shrink-0 text-muted-foreground" />
                      </button>
                    );
                  })}
                </div>
              </section>
              <section>
                <h3 className="mb-2 font-semibold">Policy floor</h3>
                <div className="grid gap-2">
                  {finding.policyReferences.map((reference) => {
                    const rule = caseData.policyPack.rules.find(
                      ({ id }) => id === reference.ruleId,
                    );
                    return (
                      <div
                        key={reference.ruleId}
                        className="rounded-md border border-border px-3 py-2"
                      >
                        <p className="font-medium">{reference.label}</p>
                        <p className="mt-1 text-sm text-muted-foreground">{rule?.guidance}</p>
                      </div>
                    );
                  })}
                </div>
              </section>
              {finding.gaps.length > 0 && (
                <section>
                  <h3 className="mb-2 font-semibold">Unresolved gaps</h3>
                  <ul className="grid gap-1 text-ui text-muted-foreground">
                    {finding.gaps.map((gap) => (
                      <li key={gap} className="flex gap-2">
                        <AlertTriangleIcon className="mt-1 size-3.5 shrink-0 text-status-yellow" />
                        {gap}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function QuestionAnswerDialog({
  question,
  onOpenChange,
  onSubmit,
  busy = false,
}: {
  question: StakeholderQuestion | null;
  onOpenChange: (open: boolean) => void;
  onSubmit: (response: string, answeredBy: string) => Promise<unknown>;
  busy?: boolean;
}) {
  const [response, setResponse] = useState("");
  const [answeredBy, setAnsweredBy] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = busy || submitting;
  useEffect(() => {
    if (question) {
      setResponse(question.response ?? "");
      setAnsweredBy(question.answeredBy ?? "");
    }
  }, [question]);
  useEffect(() => {
    if (!question) setError(null);
  }, [question]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(response, answeredBy);
      onOpenChange(false);
    } catch (cause) {
      setError(submissionError(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={question !== null} onOpenChange={(next) => !pending && onOpenChange(next)}>
      <DialogContent className="max-w-xl">
        {question && (
          <form onSubmit={submit}>
            <DialogHeader>
              <Badge variant="outline" className="mb-1">
                {question.stakeholder}
              </Badge>
              <DialogTitle>Record stakeholder answer</DialogTitle>
              <DialogDescription>{question.text}</DialogDescription>
            </DialogHeader>
            <div className="mt-5 grid gap-4">
              <label htmlFor="dpia-stakeholder-answer">
                <span className="mb-1.5 block text-sm font-medium">Answer</span>
                <Textarea
                  id="dpia-stakeholder-answer"
                  aria-label="Stakeholder answer"
                  value={response}
                  onChange={(event) => setResponse(event.target.value)}
                  className="min-h-28"
                  required
                />
              </label>
              <label htmlFor="dpia-answered-by">
                <span className="mb-1.5 block text-sm font-medium">Answered by</span>
                <Input
                  id="dpia-answered-by"
                  aria-label="Answered by"
                  value={answeredBy}
                  onChange={(event) => setAnsweredBy(event.target.value)}
                  placeholder="Synthetic stakeholder name and role"
                  required
                />
              </label>
              <p className="text-sm text-muted-foreground">
                The response is stored as attributed synthetic evidence and may make dependent
                findings stale until reassessed.
              </p>
              {error && (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              )}
            </div>
            <DialogFooter className="mt-6">
              <Button
                type="button"
                variant="outline"
                disabled={pending}
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={pending || response.trim().length < 10 || answeredBy.trim().length < 2}
              >
                {pending && <Loader2Icon className="animate-spin" />}
                {pending ? "Recording" : "Record answer"}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

export type OfficerAction = OfficerDecision["action"];

export function OfficerDecisionDialog({
  action,
  caseData,
  onOpenChange,
  onSubmit,
  busy = false,
}: {
  action: OfficerAction | null;
  caseData: DpiaCaseSnapshot;
  onOpenChange: (open: boolean) => void;
  onSubmit: (
    decision: Omit<OfficerDecision, "processingModelVersion" | "policyPackVersion">,
  ) => Promise<unknown>;
  busy?: boolean;
}) {
  const [outcome, setOutcome] = useState<OfficerDecision["outcome"]>(caseData.recommendation);
  const [rationale, setRationale] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = busy || submitting;
  useEffect(() => {
    if (!action) return;
    setOutcome(
      action === "more-information"
        ? "more-information-required"
        : action === "rejected"
          ? "no-full-dpia-indicated"
          : caseData.recommendation,
    );
    setRationale(
      action === "accepted"
        ? "I accept the recommendation to progress to a full DPIA, subject to the recorded gaps and mitigations."
        : "",
    );
  }, [action, caseData.recommendation]);
  useEffect(() => {
    if (!action) setError(null);
  }, [action]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!action || pending) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit({
        action,
        outcome,
        rationale,
        officer: "Alex Morgan",
        decidedAt: new Date().toISOString(),
      });
      onOpenChange(false);
    } catch (cause) {
      setError(submissionError(cause));
    } finally {
      setSubmitting(false);
    }
  }

  const title =
    action === "accepted"
      ? "Accept screening recommendation"
      : action === "edited"
        ? "Edit screening determination"
        : action === "rejected"
          ? "Reject screening recommendation"
          : "Ask for more information";

  return (
    <Dialog open={action !== null} onOpenChange={(next) => !pending && onOpenChange(next)}>
      <DialogContent className="max-w-xl">
        {action && (
          <form onSubmit={submit}>
            <DialogHeader>
              <div className="mb-1 flex items-center gap-2">
                <ShieldCheckIcon className="size-4 text-status-blue" />
                <span className="text-sm font-medium text-muted-foreground">
                  Human decision gate
                </span>
              </div>
              <DialogTitle>{title}</DialogTitle>
              <DialogDescription>
                You are deciding as the synthetic Privacy Officer, Alex Morgan. The specialist
                recommendation remains visible in the audit trail.
              </DialogDescription>
            </DialogHeader>
            <div className="mt-5 grid gap-4">
              {action === "edited" && (
                <label htmlFor="dpia-officer-outcome">
                  <span className="mb-1.5 block text-sm font-medium">Determination</span>
                  <Select
                    value={outcome}
                    onValueChange={(value) => setOutcome(value as OfficerDecision["outcome"])}
                  >
                    <SelectTrigger id="dpia-officer-outcome" className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="full-dpia-likely">Full DPIA likely</SelectItem>
                      <SelectItem value="no-full-dpia-indicated">No full DPIA indicated</SelectItem>
                      <SelectItem value="more-information-required">
                        More information required
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </label>
              )}
              <label htmlFor="dpia-officer-rationale">
                <span className="mb-1.5 block text-sm font-medium">Rationale</span>
                <Textarea
                  id="dpia-officer-rationale"
                  aria-label="Officer rationale"
                  value={rationale}
                  onChange={(event) => setRationale(event.target.value)}
                  className="min-h-28"
                  required
                />
              </label>
              <div className="grid grid-cols-2 gap-3 rounded-md bg-muted/50 p-3 text-sm">
                <div>
                  <span className="block text-muted-foreground">Processing model</span>
                  <span className="font-medium">v{caseData.processingModel.version}</span>
                </div>
                <div>
                  <span className="block text-muted-foreground">Policy pack</span>
                  <span className="font-medium">{caseData.policyPack.version}</span>
                </div>
              </div>
              {error && (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              )}
            </div>
            <DialogFooter className="mt-6">
              <Button
                type="button"
                variant="outline"
                disabled={pending}
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                variant={action === "rejected" ? "destructive" : "default"}
                disabled={pending || rationale.trim().length < 10}
              >
                {pending && <Loader2Icon className="animate-spin" />}
                {pending ? "Recording" : "Record decision"}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

export type LiveRunState =
  | { status: "idle"; message: string }
  | { status: "running"; message: string }
  | { status: "failed"; message: string }
  | { status: "completed"; message: string; sessionId: string };

export function AgentActivityDialog({
  open,
  onOpenChange,
  activity,
  liveRun,
  onRunLive,
  onReplaySnapshot,
  onReset,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  activity: AgentActivity[];
  liveRun: LiveRunState;
  onRunLive: () => void;
  onReplaySnapshot: () => void;
  onReset: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[88vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Agent activity</DialogTitle>
          <DialogDescription>
            Professional mandates and execution details stay here so the case cockpit remains
            focused on evidence and decisions.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          {activity.map((item, index) => (
            <div
              key={item.id}
              className="grid grid-cols-[32px_1fr] gap-3 rounded-lg border border-border p-3"
            >
              <div className="flex size-8 items-center justify-center rounded-md bg-muted text-muted-foreground">
                {index === 2 ? (
                  <ShieldCheckIcon className="size-4" />
                ) : index === 1 ? (
                  <ScaleIcon className="size-4" />
                ) : (
                  <BotIcon className="size-4" />
                )}
              </div>
              <div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="font-medium">{item.role}</p>
                  <DpiaStatus status={item.status === "completed" ? "confirmed" : item.status} />
                </div>
                <p className="mt-1 text-sm text-muted-foreground">{item.task}</p>
                <p className="mt-2 text-ui">{item.detail}</p>
              </div>
            </div>
          ))}
        </div>

        <section className="mt-2 border-t border-border pt-5">
          <div className="mb-5 rounded-lg border border-border bg-muted/20 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h3 className="font-semibold">Validated snapshot replay</h3>
                <p className="mt-1 max-w-lg text-sm text-muted-foreground">
                  Reapply the reviewed, deterministic role artifacts to the current processing-model
                  version. This clears stale markers without claiming a live model run.
                </p>
              </div>
              <Button type="button" variant="outline" onClick={onReplaySnapshot}>
                Replay validated snapshot
              </Button>
            </div>
          </div>
          <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="font-semibold">Live model re-run</h3>
              <p className="mt-1 max-w-lg text-sm text-muted-foreground">
                Sends the current synthetic processing model to a separately configured, labelled
                root session. Live output never replaces this validated snapshot automatically.
              </p>
            </div>
            <Badge variant="outline">Experimental</Badge>
          </div>
          <output
            className="flex items-start gap-3 rounded-md border border-border bg-muted/30 px-3 py-3"
            aria-live="polite"
          >
            {liveRun.status === "running" ? (
              <Loader2Icon className="mt-0.5 size-4 shrink-0 animate-spin text-status-blue" />
            ) : liveRun.status === "failed" ? (
              <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-status-red" />
            ) : liveRun.status === "completed" ? (
              <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-status-green" />
            ) : (
              <BotIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            )}
            <p className="text-ui">{liveRun.message}</p>
          </output>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button
              type="button"
              onClick={onRunLive}
              loading={liveRun.status === "running"}
              disabled={liveRun.status === "running"}
            >
              {liveRun.status === "failed" ? "Retry live run" : "Re-run investigation"}
            </Button>
            {liveRun.status === "completed" && (
              <Button asChild variant="outline">
                <Link to={`/c/${liveRun.sessionId}`}>
                  Open live session
                  <ExternalLinkIcon data-icon="inline-end" />
                </Link>
              </Button>
            )}
            <Button type="button" variant="ghost" onClick={onReset}>
              <RotateCcwIcon data-icon="inline-start" />
              Reset case to validated seed
            </Button>
          </div>
        </section>
      </DialogContent>
    </Dialog>
  );
}

export function DecisionPackUnavailableDialog({
  message,
  onOpenChange,
}: {
  message: string | null;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={message !== null} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <div className="mb-1 flex items-center gap-2 text-status-yellow">
            <FileTextIcon className="size-4" />
            Decision pack gate
          </div>
          <DialogTitle>Decision pack is not ready</DialogTitle>
          <DialogDescription>{message}</DialogDescription>
        </DialogHeader>
      </DialogContent>
    </Dialog>
  );
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}
