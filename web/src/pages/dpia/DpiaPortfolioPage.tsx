import {
  AlertTriangleIcon,
  ArrowUpRightIcon,
  FileSearchIcon,
  PlusIcon,
  ShieldCheckIcon,
} from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useDpiaRequests } from "@/hooks/useDpiaRequests";
import { calculateReadiness } from "@/lib/dpia/readiness";
import { readinessDefinitions } from "@/lib/dpia/seed";
import { Link } from "@/lib/routing";
import { useDpiaCase } from "./useDpiaCase";

export function DpiaPortfolioPage() {
  const { caseData } = useDpiaCase("student-success-alert");
  const requests = useDpiaRequests();
  const incomingRequests = (requests.data ?? []).filter(
    (summary) => summary.status === "submitted" && summary.requestId !== null,
  );
  const readiness = calculateReadiness(
    caseData.processingModel,
    caseData.evidence,
    caseData.contradictions,
    readinessDefinitions,
    caseData.determinations,
  );
  const outstandingQuestions = caseData.questions.filter(
    ({ status }) => status !== "answered",
  ).length;
  const outstandingFacts = caseData.processingModel.facts.filter(
    ({ status }) => status === "missing" || status === "stale",
  ).length;

  return (
    <PageScroll
      maxWidthClassName="max-w-[1440px]"
      contentClassName="px-5 md:px-8"
      data-testid="dpia-portfolio"
    >
      <header className="mb-6 flex flex-wrap items-end justify-between gap-4 border-b border-border pb-5">
        <div className="min-w-0">
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-muted-foreground">
            <ShieldCheckIcon className="size-4" />
            Privacy operations
          </div>
          <h1 className="text-2xl font-semibold tracking-normal">DPIA Investigation Desk</h1>
          <p className="mt-1 max-w-2xl text-ui text-muted-foreground">
            Investigate proposed processing until the evidence is sufficient for an accountable
            human decision.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button asChild variant="outline" componentId="dpia.request_dpia">
            <Link to="/dpia/request">
              <FileSearchIcon data-icon="inline-start" />
              Request a DPIA
            </Link>
          </Button>
          <Button asChild componentId="dpia.new_assessment">
            <Link to="/dpia/new">
              <PlusIcon data-icon="inline-start" />
              New assessment
            </Link>
          </Button>
        </div>
      </header>

      <div className="mb-5 flex items-start gap-3 rounded-lg border border-border bg-muted/30 px-4 py-3 text-ui">
        <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-status-yellow" />
        <div>
          <p className="font-medium">Synthetic demonstration workspace</p>
          <p className="text-muted-foreground">
            Do not upload real student, disability, wellbeing, hardship, or attainment data. This
            environment is not approved for live university records.
          </p>
        </div>
      </div>

      <section
        aria-labelledby="incoming-requests-heading"
        className="mb-6"
        data-testid="dpia-incoming-requests"
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 id="incoming-requests-heading" className="text-ui font-semibold">
            Incoming requests
          </h2>
          <span className="text-sm text-muted-foreground">
            {incomingRequests.length === 0
              ? "None awaiting triage"
              : `${incomingRequests.length} awaiting triage`}
          </span>
        </div>
        {incomingRequests.length === 0 ? (
          <p className="rounded-lg border border-border px-4 py-3 text-sm text-muted-foreground">
            Stakeholder requests submitted through the DPIA agent appear here for triage.
          </p>
        ) : (
          <ul className="space-y-2">
            {incomingRequests.map((summary) => (
              <li
                key={summary.sessionId}
                className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border px-4 py-3"
              >
                <div className="min-w-0">
                  <p className="truncate font-medium">
                    {summary.request?.project.title ?? summary.requestId}
                  </p>
                  <p className="text-sm text-muted-foreground">
                    {summary.request
                      ? `${summary.request.requester.name} (${summary.request.requester.team}) · submitted ${summary.request.submitted_at.slice(0, 10)}`
                      : "Awaiting structured request"}
                  </p>
                </div>
                <Button asChild variant="outline" size="sm">
                  <Link to={`/dpia/requests/${summary.requestId}`}>
                    Review request
                    <ArrowUpRightIcon data-icon="inline-end" />
                  </Link>
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="active-cases-heading">
        <div className="mb-3 flex items-center justify-between">
          <h2 id="active-cases-heading" className="text-ui font-semibold">
            Active cases
          </h2>
          <span className="text-sm text-muted-foreground">1 assessment</span>
        </div>

        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <div className="hidden grid-cols-[minmax(210px,2fr)_minmax(110px,1fr)_115px_125px_100px_80px_90px] gap-3 border-b border-border bg-muted/40 px-4 py-2 text-sm font-medium text-muted-foreground xl:grid">
            <span>Processing activity</span>
            <span>Owner</span>
            <span>Stage</span>
            <span>Decision readiness</span>
            <span>Risk signal</span>
            <span>Questions</span>
            <span>Last updated</span>
          </div>
          <Link
            to="/dpia/cases/student-success-alert"
            className="group grid gap-4 px-4 py-4 transition-colors hover:bg-muted/40 xl:grid-cols-[minmax(210px,2fr)_minmax(110px,1fr)_115px_125px_100px_80px_90px] xl:items-center xl:gap-3"
          >
            <div className="flex min-w-0 items-start gap-3">
              <div className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-md bg-status-blue/10 text-status-blue">
                <FileSearchIcon className="size-4" />
              </div>
              <div className="min-w-0">
                <div className="flex items-start gap-2">
                  <h3 className="min-w-0 font-medium leading-snug">{caseData.title}</h3>
                  <ArrowUpRightIcon className="size-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                  <Badge
                    variant="outline"
                    className="border-status-yellow/30 bg-status-yellow/10 text-status-yellow"
                  >
                    Synthetic data
                  </Badge>
                  <span>UK GDPR</span>
                  <span>Model v{caseData.processingModel.version}</span>
                </div>
              </div>
            </div>
            <PortfolioField label="Owner" value={caseData.owner} />
            <PortfolioField label="Stage" value={caseData.stage} />
            <div>
              <span className="mb-1 block text-sm text-muted-foreground xl:hidden">
                Decision readiness
              </span>
              <strong className="font-semibold tabular-nums">
                {readiness.answerable}/{readiness.total}
              </strong>{" "}
              <span className="text-sm text-muted-foreground">answerable</span>
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full bg-status-blue"
                  style={{ width: `${(readiness.answerable / readiness.total) * 100}%` }}
                />
              </div>
            </div>
            <div>
              <span className="mb-1 block text-sm text-muted-foreground xl:hidden">
                Risk signal
              </span>
              <Badge
                variant="outline"
                className="border-status-red/25 bg-status-red/10 text-status-red"
              >
                Potentially high
              </Badge>
            </div>
            <PortfolioField label="Outstanding questions" value={`${outstandingQuestions} open`} />
            <div>
              <span className="mb-1 block text-sm text-muted-foreground xl:hidden">
                Last updated
              </span>
              <span className="text-ui">12 Aug 2026</span>
              <span className="block text-sm text-muted-foreground">
                {outstandingFacts} missing facts
              </span>
            </div>
          </Link>
        </div>
      </section>
    </PageScroll>
  );
}

function PortfolioField({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <span className="mb-1 block text-sm text-muted-foreground xl:hidden">{label}</span>
      <span className="block truncate text-ui">{value}</span>
    </div>
  );
}
