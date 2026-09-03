import {
  AlertTriangleIcon,
  ArrowRightIcon,
  CheckCircle2Icon,
  CircleHelpIcon,
  FileSearchIcon,
  ScaleIcon,
  ShieldCheckIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { DpiaCaseSnapshot, Determination } from "@/lib/dpia/types";
import type { OfficerAction } from "./DpiaDialogs";
import { DpiaStatus } from "./DpiaStatus";

export function DpiaScreening({
  caseData,
  onFinding,
  onOfficerAction,
  onContinue,
  onAgentActivity,
}: {
  caseData: DpiaCaseSnapshot;
  onFinding: (finding: Determination) => void;
  onOfficerAction: (action: OfficerAction) => void;
  onContinue: () => void;
  onAgentActivity: () => void;
}) {
  const staleFindings = caseData.determinations.filter(
    ({ status, processingModelVersion }) =>
      status === "stale-after-change" ||
      processingModelVersion !== caseData.processingModel.version,
  );
  const accepted =
    caseData.officerDecision?.action === "accepted" &&
    caseData.officerDecision.outcome === "full-dpia-likely";
  const triggerFindings = [
    {
      label: "Systematic evaluation and profiling",
      detail: "Individual disengagement scoring is used to prioritise intervention.",
      evidence: "Operating procedure · Model card",
    },
    {
      label: "Special-category and vulnerable circumstances",
      detail:
        "Disability, wellbeing, hardship, and accommodation indicators may influence a score.",
      evidence: "Data dictionary · Disability Services response",
    },
    {
      label: "Matching datasets at scale",
      detail: "Attendance, learning, attainment, library, and support systems are combined.",
      evidence: "Data dictionary · Project intake",
    },
    {
      label: "Potential educational or welfare consequence",
      detail: "Repeated alerts may enter attendance escalation or fitness-to-study workflows.",
      evidence: "Operating procedure · Student Services response",
    },
  ];

  return (
    <div className="space-y-7">
      {staleFindings.length > 0 && (
        <div className="flex items-start gap-3 rounded-lg border border-border bg-muted/30 px-4 py-3">
          <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-status-yellow" />
          <div className="min-w-0">
            <p className="font-medium">
              {staleFindings.length} finding{staleFindings.length === 1 ? " is" : "s are"} stale
              after processing model v{caseData.processingModel.version}
            </p>
            <p className="mt-0.5 text-sm text-muted-foreground">
              The validated recommendation remains visible for comparison, but a new decision pack
              cannot be synthesised until the affected findings are reassessed.
            </p>
          </div>
          <Button type="button" variant="outline" size="sm" onClick={onAgentActivity}>
            Re-run
          </Button>
        </div>
      )}

      <section
        className="overflow-hidden rounded-lg border border-border bg-card"
        aria-labelledby="recommendation-heading"
      >
        <div className="grid gap-5 p-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(280px,0.6fr)]">
          <div>
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <Badge
                variant="outline"
                className="border-status-red/25 bg-status-red/10 text-status-red"
              >
                Screening recommendation
              </Badge>
              <Badge variant="outline">Privacy Officer verification required</Badge>
            </div>
            <h2 id="recommendation-heading" className="text-2xl font-semibold tracking-normal">
              Full DPIA likely
            </h2>
            <p className="mt-2 max-w-3xl text-ui leading-relaxed text-muted-foreground">
              Multiple high-risk indicators are supported by the synthetic evidence: systematic
              student profiling, combined educational datasets, special-category indicators,
              potentially vulnerable circumstances, and plausible effects on attendance or welfare
              decisions. This is a screening recommendation, not an automated legal determination.
            </p>
          </div>
          <div className="border-t border-border pt-4 lg:border-t-0 lg:border-l lg:pt-0 lg:pl-5">
            <div className="flex items-center gap-2">
              <ShieldCheckIcon className="size-4 text-status-green" />
              <span className="font-medium">Verifier verdict</span>
            </div>
            <p className="mt-2 font-medium">Verified with caveats</p>
            <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
              Citation coverage is complete. Whether advisory scoring materially affects students
              remains contingent on a binding support/escalation boundary.
            </p>
          </div>
        </div>
        <div className="grid border-t border-border bg-muted/20 sm:grid-cols-3">
          <RecommendationMetric label="Evidence coverage" value="8/8 findings" />
          <RecommendationMetric label="Policy pack" value={caseData.policyPack.version} />
          <RecommendationMetric
            label="Processing model"
            value={`v${caseData.processingModel.version}`}
          />
        </div>
      </section>

      <section aria-labelledby="trigger-criteria-heading">
        <div className="mb-3 flex items-end justify-between gap-3">
          <div>
            <h2 id="trigger-criteria-heading" className="font-semibold">
              Material trigger criteria
            </h2>
            <p className="mt-0.5 text-sm text-muted-foreground">
              Criteria inform professional judgement; no “two criteria” shortcut is applied.
            </p>
          </div>
          <Badge variant="outline">UK GDPR Article 35 · ICO guidance</Badge>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {triggerFindings.map((trigger) => (
            <article key={trigger.label} className="rounded-lg border border-border bg-card p-4">
              <CheckCircle2Icon className="mb-3 size-5 text-status-red" />
              <h3 className="font-medium leading-snug">{trigger.label}</h3>
              <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{trigger.detail}</p>
              <p className="mt-3 border-t border-border pt-2 text-sm font-medium text-muted-foreground">
                {trigger.evidence}
              </p>
            </article>
          ))}
        </div>
      </section>

      <section aria-labelledby="rules-judgement-heading">
        <h2 id="rules-judgement-heading" className="mb-3 font-semibold">
          Rules and professional judgement
        </h2>
        <div className="grid overflow-hidden rounded-lg border border-border bg-card lg:grid-cols-2">
          <div className="p-5 lg:border-r lg:border-border">
            <div className="mb-3 flex items-center gap-2">
              <FileSearchIcon className="size-4 text-status-blue" />
              <h3 className="font-semibold">Policy criteria</h3>
            </div>
            <ul className="grid gap-3 text-ui">
              <RuleLine status="Met" text="Systematic evaluation or scoring of students" />
              <RuleLine status="Met" text="Matching or combining data across university systems" />
              <RuleLine
                status="Met"
                text="Special-category data and potentially vulnerable people"
              />
              <RuleLine
                status="Unclear"
                text="Systematic and extensive evaluation producing significant effects"
              />
              <RuleLine status="Unclear" text="Residual high risk after all planned mitigations" />
            </ul>
          </div>
          <div className="border-t border-border p-5 lg:border-t-0">
            <div className="mb-3 flex items-center gap-2">
              <ScaleIcon className="size-4 text-status-yellow" />
              <h3 className="font-semibold">Professional judgement</h3>
            </div>
            <p className="text-ui leading-relaxed text-muted-foreground">
              The assessor considers the combined scale, sensitivity, monitoring, innovative
              technology, and possible escalation consequences sufficient to recommend a full DPIA.
              The verifier agrees with progression but preserves uncertainty about legal
              significance until staff discretion and escalation controls are binding.
            </p>
            <div className="mt-4 rounded-md bg-muted px-3 py-2">
              <p className="text-sm font-medium">Preserved disagreement</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Advisory scoring may materially affect students in practice even if no action is
                fully automated.
              </p>
            </div>
          </div>
        </div>
      </section>

      <section aria-labelledby="findings-heading">
        <div className="mb-3 flex items-end justify-between gap-3">
          <div>
            <h2 id="findings-heading" className="font-semibold">
              Determination record
            </h2>
            <p className="mt-0.5 text-sm text-muted-foreground">
              Open any row to inspect evidence, policy, reasoning, disagreement, and gaps.
            </p>
          </div>
          <Badge variant="outline">{caseData.determinations.length} dimensions</Badge>
        </div>
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          {caseData.determinations.map((finding, index) => (
            <button
              key={finding.id}
              type="button"
              onClick={() => onFinding(finding)}
              className={`grid w-full gap-3 px-4 py-3 text-left transition-colors hover:bg-muted/40 md:grid-cols-[minmax(220px,0.8fr)_100px_minmax(0,1.2fr)_auto] md:items-center ${index > 0 ? "border-t border-border" : ""}`}
            >
              <span className="font-medium leading-snug">{finding.question}</span>
              <span className="text-sm font-medium uppercase text-muted-foreground">
                {finding.outcome.replace("-", " ")}
              </span>
              <span className="line-clamp-2 text-sm text-muted-foreground">
                {finding.reasoning}
              </span>
              <span className="flex items-center gap-2">
                <DpiaStatus status={finding.status} />
                <ArrowRightIcon className="size-4 text-muted-foreground" />
              </span>
            </button>
          ))}
        </div>
      </section>

      <section
        className="rounded-lg border border-border bg-card p-5"
        aria-labelledby="officer-gate-heading"
      >
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
          <div>
            <div className="mb-2 flex items-center gap-2">
              <ShieldCheckIcon className="size-5 text-status-blue" />
              <h2 id="officer-gate-heading" className="text-lg font-semibold">
                Privacy Officer decision
              </h2>
            </div>
            {caseData.officerDecision ? (
              <div>
                <p className="font-medium">
                  {caseData.officerDecision.officer} recorded:{" "}
                  {caseData.officerDecision.outcome.replaceAll("-", " ")}
                </p>
                <p className="mt-1 text-ui text-muted-foreground">
                  {caseData.officerDecision.rationale}
                </p>
              </div>
            ) : (
              <p className="max-w-3xl text-ui text-muted-foreground">
                Review the cited recommendation and verifier caveats. Accepting carries the same
                processing facts and evidence into the lightweight full DPIA; editing or rejecting
                requires a rationale.
              </p>
            )}
          </div>
          <div className="grid gap-2 sm:grid-cols-2 lg:flex lg:max-w-[420px] lg:flex-wrap lg:justify-end">
            <Button
              type="button"
              className="w-full lg:w-auto"
              onClick={() => onOfficerAction("accepted")}
              disabled={staleFindings.length > 0}
            >
              Accept recommendation
            </Button>
            <Button
              type="button"
              variant="outline"
              className="w-full lg:w-auto"
              onClick={() => onOfficerAction("edited")}
            >
              Edit determination
            </Button>
            <Button
              type="button"
              variant="outline"
              className="w-full lg:w-auto"
              onClick={() => onOfficerAction("more-information")}
            >
              Ask for information
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="w-full lg:w-auto"
              onClick={() => onOfficerAction("rejected")}
            >
              Reject
            </Button>
          </div>
        </div>
        {staleFindings.length > 0 && (
          <p className="mt-3 text-sm text-status-yellow">
            Accept is disabled until stale findings are reassessed.
          </p>
        )}
        {accepted && (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
            <span className="flex items-center gap-2 text-ui font-medium text-status-green">
              <CheckCircle2Icon className="size-4" />
              Screening accepted and carried forward
            </span>
            <Button type="button" variant="outline" onClick={onContinue}>
              Continue to Full DPIA
              <ArrowRightIcon data-icon="inline-end" />
            </Button>
          </div>
        )}
      </section>
    </div>
  );
}

function RecommendationMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="border-b border-border px-5 py-3 last:border-b-0 sm:border-r sm:border-b-0 sm:last:border-r-0">
      <span className="block text-sm text-muted-foreground">{label}</span>
      <span className="mt-0.5 block break-words font-medium">{value}</span>
    </div>
  );
}

function RuleLine({ status, text }: { status: "Met" | "Unclear"; text: string }) {
  return (
    <li className="flex items-start gap-2">
      {status === "Met" ? (
        <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-status-green" />
      ) : (
        <CircleHelpIcon className="mt-0.5 size-4 shrink-0 text-status-yellow" />
      )}
      <span>
        <span className="font-medium">{status}</span>
        <span className="block text-sm text-muted-foreground">{text}</span>
      </span>
    </li>
  );
}
