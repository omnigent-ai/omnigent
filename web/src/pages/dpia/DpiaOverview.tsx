import {
  AlertTriangleIcon,
  ArrowRightIcon,
  BotIcon,
  FileSearchIcon,
  PencilIcon,
  ScaleIcon,
  ShieldCheckIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { DpiaCaseSnapshot, ReadinessSummary } from "@/lib/dpia/types";
import { DpiaStatus } from "./DpiaStatus";

export function DpiaOverview({
  caseData,
  readiness,
  onEditIntake,
  onFinding,
  onAgentActivity,
  onNavigate,
}: {
  caseData: DpiaCaseSnapshot;
  readiness: ReadinessSummary;
  onEditIntake: () => void;
  onFinding: (findingId: string) => void;
  onAgentActivity: () => void;
  onNavigate: (tab: string) => void;
}) {
  const summaryFacts = [
    "purpose",
    "data-subjects",
    "data-sources",
    "vendor",
    "legal-basis",
    "retention",
  ]
    .map((factId) => caseData.processingModel.facts.find(({ id }) => id === factId))
    .filter((fact) => fact !== undefined);
  const openQuestions = caseData.questions.filter(({ status }) => status !== "answered");
  const missingFacts = caseData.processingModel.facts.filter(({ status }) => status === "missing");

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1.55fr)_minmax(320px,0.8fr)]">
      <div className="min-w-0 space-y-7">
        <section aria-labelledby="processing-summary-heading">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 id="processing-summary-heading" className="font-semibold">
                Processing summary
              </h2>
              <p className="mt-0.5 text-sm text-muted-foreground">
                Current material facts in processing model v{caseData.processingModel.version}
              </p>
            </div>
            <Button type="button" variant="outline" size="sm" onClick={onEditIntake}>
              <PencilIcon data-icon="inline-start" />
              Edit intake
            </Button>
          </div>
          <dl className="grid overflow-hidden rounded-lg border border-border bg-card md:grid-cols-2">
            {summaryFacts.map((fact, index) => (
              <div
                key={fact.id}
                className={`min-w-0 p-4 ${index < summaryFacts.length - 2 ? "border-b border-border" : ""} ${index % 2 === 0 ? "md:border-r md:border-border" : ""}`}
              >
                <dt className="mb-1 text-sm font-medium text-muted-foreground">{fact.label}</dt>
                <dd className="text-ui leading-relaxed">{fact.value || "Not evidenced"}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section aria-labelledby="contradictions-heading">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div>
              <h2 id="contradictions-heading" className="font-semibold">
                Material contradictions
              </h2>
              <p className="mt-0.5 text-sm text-muted-foreground">
                Conflicts remain visible even when the officer resolves them.
              </p>
            </div>
            <Badge variant="outline">{caseData.contradictions.length} cited</Badge>
          </div>
          <div className="grid gap-3 lg:grid-cols-2">
            {caseData.contradictions.map((contradiction) => (
              <article
                key={contradiction.id}
                className="rounded-lg border border-border bg-card p-4"
              >
                <div className="mb-3 flex items-start justify-between gap-3">
                  <div className="flex min-w-0 items-start gap-2">
                    <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-status-yellow" />
                    <h3 className="font-medium leading-snug">{contradiction.title}</h3>
                  </div>
                  <DpiaStatus status={contradiction.resolved ? "confirmed" : "needs-judgement"} />
                </div>
                <p className="text-ui leading-relaxed text-muted-foreground">
                  {contradiction.summary}
                </p>
                {contradiction.resolution && (
                  <p className="mt-3 rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
                    {contradiction.resolution}
                  </p>
                )}
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {contradiction.sourceReferences.map((reference) => {
                    const evidence = caseData.evidence.find(
                      ({ id }) => id === reference.evidenceId,
                    );
                    return (
                      <Badge key={reference.evidenceId} variant="secondary">
                        {evidence?.title ?? reference.evidenceId}
                      </Badge>
                    );
                  })}
                </div>
              </article>
            ))}
          </div>
        </section>

        <section aria-labelledby="roles-heading">
          <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 id="roles-heading" className="font-semibold">
                Independent professional roles
              </h2>
              <p className="mt-0.5 text-sm text-muted-foreground">
                Separate mandates, structured artifacts, and a blinded initial verification.
              </p>
            </div>
            <Button type="button" variant="ghost" size="sm" onClick={onAgentActivity}>
              Agent activity
              <ArrowRightIcon data-icon="inline-end" />
            </Button>
          </div>
          <div className="overflow-hidden rounded-lg border border-border bg-card">
            {caseData.agentActivity.map((activity, index) => (
              <div
                key={activity.id}
                className={`grid grid-cols-[32px_minmax(0,1fr)_auto] items-start gap-3 px-4 py-3 ${index > 0 ? "border-t border-border" : ""}`}
              >
                <div className="flex size-8 items-center justify-center rounded-md bg-muted text-muted-foreground">
                  {index === 0 ? (
                    <FileSearchIcon className="size-4" />
                  ) : index === 1 ? (
                    <ScaleIcon className="size-4" />
                  ) : (
                    <ShieldCheckIcon className="size-4" />
                  )}
                </div>
                <div className="min-w-0">
                  <p className="font-medium">{activity.role}</p>
                  <p className="mt-0.5 text-sm text-muted-foreground">{activity.task}</p>
                </div>
                <DpiaStatus
                  status={activity.status === "completed" ? "confirmed" : activity.status}
                />
              </div>
            ))}
          </div>
        </section>
      </div>

      <aside className="min-w-0 space-y-6">
        <section aria-labelledby="readiness-heading">
          <div className="mb-3 flex items-end justify-between gap-3">
            <div>
              <h2 id="readiness-heading" className="font-semibold">
                Decision readiness
              </h2>
              <p className="mt-0.5 text-sm text-muted-foreground">
                Inspectable, not model confidence
              </p>
            </div>
            <strong className="text-xl font-semibold tabular-nums">
              {readiness.answerable}/{readiness.total}
            </strong>
          </div>
          <div className="overflow-hidden rounded-lg border border-border bg-card">
            {readiness.dimensions.map((dimension, index) => {
              const finding = caseData.determinations.find(
                ({ dimensionId }) => dimensionId === dimension.id,
              );
              return (
                <button
                  key={dimension.id}
                  type="button"
                  onClick={() => finding && onFinding(finding.id)}
                  className={`grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-3 py-3 text-left transition-colors hover:bg-muted/50 ${index > 0 ? "border-t border-border" : ""}`}
                >
                  <span className="min-w-0">
                    <span className="block font-medium leading-snug">{dimension.label}</span>
                    <span className="mt-0.5 block truncate text-sm text-muted-foreground">
                      {dimension.detail}
                    </span>
                  </span>
                  <DpiaStatus status={dimension.status} />
                </button>
              );
            })}
          </div>
        </section>

        <section aria-labelledby="worklist-heading">
          <div className="mb-3 flex items-center justify-between">
            <h2 id="worklist-heading" className="font-semibold">
              Investigation worklist
            </h2>
            <Badge variant="outline">{missingFacts.length + openQuestions.length} open</Badge>
          </div>
          <div className="rounded-lg border border-border bg-card p-4">
            <div className="grid grid-cols-2 gap-4 border-b border-border pb-4">
              <div>
                <span className="block text-2xl font-semibold tabular-nums">
                  {missingFacts.length}
                </span>
                <span className="text-sm text-muted-foreground">Missing facts</span>
              </div>
              <div>
                <span className="block text-2xl font-semibold tabular-nums">
                  {openQuestions.length}
                </span>
                <span className="text-sm text-muted-foreground">Open questions</span>
              </div>
            </div>
            <ul className="mt-3 grid gap-2 text-ui">
              {missingFacts.slice(0, 4).map((fact) => (
                <li key={fact.id} className="flex items-start gap-2">
                  <span className="mt-2 size-1.5 shrink-0 rounded-full bg-status-red" />
                  <span>
                    <span className="font-medium">{fact.label}</span>
                    <span className="block text-sm text-muted-foreground">{fact.section}</span>
                  </span>
                </li>
              ))}
            </ul>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-4 w-full"
              onClick={() => onNavigate("evidence")}
            >
              Open evidence & questions
            </Button>
          </div>
        </section>

        <div className="flex items-start gap-3 rounded-lg border border-border bg-muted/30 px-4 py-3">
          <BotIcon className="mt-0.5 size-4 shrink-0 text-status-blue" />
          <p className="text-sm text-muted-foreground">
            The validated snapshot opens instantly. A live re-run is separately labelled and never
            replaces reviewed findings without an explicit officer action.
          </p>
        </div>
      </aside>
    </div>
  );
}
