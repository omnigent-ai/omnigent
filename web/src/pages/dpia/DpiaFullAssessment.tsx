import {
  AlertTriangleIcon,
  CalendarClockIcon,
  CheckCircle2Icon,
  ClipboardCheckIcon,
  ShieldAlertIcon,
  UsersIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { DpiaCaseSnapshot, RiskItem } from "@/lib/dpia/types";

export function DpiaFullAssessment({ caseData }: { caseData: DpiaCaseSnapshot }) {
  const accepted =
    caseData.officerDecision?.action === "accepted" &&
    caseData.officerDecision.outcome === "full-dpia-likely";
  const purpose = caseData.processingModel.facts.find(({ id }) => id === "purpose")?.value;
  const dataSubjects = caseData.processingModel.facts.find(
    ({ id }) => id === "data-subjects",
  )?.value;
  const necessity = caseData.processingModel.facts.find(({ id }) => id === "necessity")?.value;
  const alternatives = caseData.processingModel.facts.find(
    ({ id }) => id === "less-intrusive-means",
  )?.value;

  return (
    <div className="space-y-7">
      <div className="flex items-start gap-3 rounded-lg border border-border bg-muted/30 px-4 py-3">
        {accepted ? (
          <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-status-green" />
        ) : (
          <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-status-yellow" />
        )}
        <div>
          <p className="font-medium">
            {accepted ? "Screening facts carried forward" : "Draft prepared from screening facts"}
          </p>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Processing model v{caseData.processingModel.version} and all linked evidence are reused
            without re-entry.
            {!accepted &&
              " The Privacy Officer must accept or edit the screening determination before finalisation."}
          </p>
        </div>
      </div>

      <section aria-labelledby="full-description-heading">
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 id="full-description-heading" className="font-semibold">
              Processing and consultation
            </h2>
            <p className="mt-0.5 text-sm text-muted-foreground">
              Read-only carry-forward from the current case record
            </p>
          </div>
          <Badge variant="outline">No repeated intake fields</Badge>
        </div>
        <div className="grid overflow-hidden rounded-lg border border-border bg-card lg:grid-cols-2">
          <FullDpiaSection
            icon={ClipboardCheckIcon}
            title="Processing description"
            body={`${purpose} ${dataSubjects}`}
          />
          <FullDpiaSection
            icon={UsersIcon}
            title="Stakeholder and data-subject consultation"
            body="Student Services, Registry, IT Security, Disability Services, academic schools, and Procurement supplied attributed answers. Direct student consultation remains outstanding."
          />
          <FullDpiaSection
            icon={ShieldAlertIcon}
            title="Necessity and proportionality"
            body={`${necessity} ${alternatives}`}
          />
          <FullDpiaSection
            icon={CalendarClockIcon}
            title="Review and change triggers"
            body="Review before launch, after bias testing, when hosting or subprocessors change, when score use expands, or no later than 12 months after approval."
          />
        </div>
      </section>

      <section aria-labelledby="risk-register-heading">
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 id="risk-register-heading" className="font-semibold">
              Risks to students’ rights and freedoms
            </h2>
            <p className="mt-0.5 text-sm text-muted-foreground">
              Inherent risk, current controls, additional mitigation, and residual risk remain
              distinct.
            </p>
          </div>
          <div className="flex gap-2">
            <Badge
              variant="outline"
              className="border-status-red/25 bg-status-red/10 text-status-red"
            >
              {caseData.risks.filter(({ inherentRating }) => inherentRating === "critical").length}{" "}
              critical inherent
            </Badge>
            <Badge
              variant="outline"
              className="border-status-yellow/30 bg-status-yellow/10 text-status-yellow"
            >
              {caseData.risks.filter(({ residualRating }) => residualRating === "high").length} high
              residual
            </Badge>
          </div>
        </div>
        <div className="grid gap-3 xl:grid-cols-2 2xl:hidden">
          {caseData.risks.map((risk) => (
            <RiskCard key={risk.id} risk={risk} />
          ))}
        </div>
        <div className="hidden overflow-x-auto rounded-lg border border-border bg-card 2xl:block">
          <table className="w-full min-w-[1180px] border-collapse text-left text-ui">
            <thead className="bg-muted/40 text-sm text-muted-foreground">
              <tr>
                <th className="px-4 py-2.5 font-medium">Student harm</th>
                <th className="px-4 py-2.5 font-medium">Affected subjects</th>
                <th className="px-4 py-2.5 font-medium">Inherent</th>
                <th className="px-4 py-2.5 font-medium">Existing controls</th>
                <th className="px-4 py-2.5 font-medium">Additional mitigation</th>
                <th className="px-4 py-2.5 font-medium">Residual</th>
                <th className="px-4 py-2.5 font-medium">Owner / due</th>
              </tr>
            </thead>
            <tbody>
              {caseData.risks.map((risk) => (
                <RiskRow key={risk.id} risk={risk} />
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="grid gap-4 lg:grid-cols-2" aria-labelledby="residual-gate-heading">
        <div className="rounded-lg border border-border bg-card p-5">
          <h2 id="residual-gate-heading" className="font-semibold">
            Residual high-risk gate
          </h2>
          <p className="mt-2 text-ui leading-relaxed text-muted-foreground">
            Discrimination, function creep, and transfer exposure remain high until evidence and
            binding mitigations are complete. Deployment must not proceed while a high residual risk
            cannot be reduced.
          </p>
          <div className="mt-4 flex items-center gap-2 text-status-red">
            <ShieldAlertIcon className="size-4" />
            <span className="font-medium">Launch gate: blocked pending mitigation evidence</span>
          </div>
        </div>
        <div className="rounded-lg border border-border bg-card p-5">
          <h2 className="font-semibold">Article 36 prior consultation</h2>
          <p className="mt-2 text-ui leading-relaxed text-muted-foreground">
            Not yet determined. The Privacy Officer must reassess after planned controls and
            representative testing. If high residual risk remains and cannot be reduced, escalate
            for prior consultation before processing.
          </p>
          <Badge
            variant="outline"
            className="mt-4 border-status-yellow/30 bg-status-yellow/10 text-status-yellow"
          >
            Decision deferred until mitigations are tested
          </Badge>
        </div>
      </section>
    </div>
  );
}

function FullDpiaSection({
  icon: Icon,
  title,
  body,
}: {
  icon: typeof ClipboardCheckIcon;
  title: string;
  body: string;
}) {
  return (
    <div className="border-b border-border p-5 odd:lg:border-r">
      <div className="mb-2 flex items-center gap-2">
        <Icon className="size-4 text-muted-foreground" />
        <h3 className="font-semibold">{title}</h3>
      </div>
      <p className="text-ui leading-relaxed text-muted-foreground">{body}</p>
    </div>
  );
}

function RiskRow({ risk }: { risk: RiskItem }) {
  return (
    <tr className="border-t border-border align-top">
      <td className="max-w-[230px] px-4 py-3">
        <span className="font-medium">{risk.harm}</span>
        <span className="mt-1 block text-sm text-muted-foreground">
          {risk.likelihood} likelihood · {risk.severity} severity
        </span>
      </td>
      <td className="max-w-[210px] px-4 py-3 text-sm text-muted-foreground">
        {risk.affectedSubjects}
      </td>
      <td className="px-4 py-3">
        <RiskBadge rating={risk.inherentRating} />
      </td>
      <td className="max-w-[220px] px-4 py-3 text-sm text-muted-foreground">
        {risk.controls.join(" · ")}
      </td>
      <td className="max-w-[280px] px-4 py-3 text-sm text-muted-foreground">{risk.mitigation}</td>
      <td className="px-4 py-3">
        <RiskBadge rating={risk.residualRating} />
      </td>
      <td className="max-w-[180px] px-4 py-3">
        <span className="block text-sm font-medium">{risk.owner}</span>
        <span className="mt-1 block text-sm text-muted-foreground">{formatDate(risk.dueDate)}</span>
      </td>
    </tr>
  );
}

function RiskCard({ risk }: { risk: RiskItem }) {
  return (
    <article className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-medium leading-snug">{risk.harm}</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            {risk.likelihood} likelihood · {risk.severity} severity
          </p>
        </div>
        <RiskBadge rating={risk.inherentRating} />
      </div>
      <dl className="mt-4 grid gap-3 text-ui">
        <div>
          <dt className="text-sm font-medium text-muted-foreground">Affected students</dt>
          <dd className="mt-0.5">{risk.affectedSubjects}</dd>
        </div>
        <div>
          <dt className="text-sm font-medium text-muted-foreground">Existing controls</dt>
          <dd className="mt-0.5">{risk.controls.join(" · ")}</dd>
        </div>
        <div className="rounded-md bg-muted px-3 py-2">
          <dt className="text-sm font-medium">Additional mitigation</dt>
          <dd className="mt-0.5 text-muted-foreground">{risk.mitigation}</dd>
        </div>
      </dl>
      <div className="mt-4 flex flex-wrap items-end justify-between gap-3 border-t border-border pt-3">
        <div>
          <span className="block text-sm text-muted-foreground">Residual risk</span>
          <RiskBadge rating={risk.residualRating} />
        </div>
        <div className="text-right text-sm">
          <span className="block font-medium">{risk.owner}</span>
          <span className="text-muted-foreground">Due {formatDate(risk.dueDate)}</span>
        </div>
      </div>
    </article>
  );
}

function RiskBadge({ rating }: { rating: RiskItem["inherentRating"] }) {
  const className =
    rating === "critical" || rating === "high"
      ? "border-status-red/25 bg-status-red/10 text-status-red"
      : rating === "medium"
        ? "border-status-yellow/30 bg-status-yellow/10 text-status-yellow"
        : "border-status-green/25 bg-status-green/10 text-status-green";
  return (
    <Badge variant="outline" className={className}>
      {rating}
    </Badge>
  );
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("en-GB", { dateStyle: "medium" }).format(new Date(value));
}
