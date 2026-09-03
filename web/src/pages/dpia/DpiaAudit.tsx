import {
  BotIcon,
  CheckCircle2Icon,
  FileClockIcon,
  HistoryIcon,
  ShieldCheckIcon,
  UserRoundIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { AuditEvent, DpiaCaseSnapshot } from "@/lib/dpia/types";

export function DpiaAudit({ caseData }: { caseData: DpiaCaseSnapshot }) {
  const events = [...caseData.audit].sort(
    (left, right) => new Date(right.timestamp).getTime() - new Date(left.timestamp).getTime(),
  );

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1.25fr)_minmax(300px,0.55fr)]">
      <section aria-labelledby="audit-timeline-heading">
        <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 id="audit-timeline-heading" className="font-semibold">
              Attributed audit timeline
            </h2>
            <p className="mt-0.5 text-sm text-muted-foreground">
              Specialist disagreement, model changes, answers, and officer overrides remain in
              sequence.
            </p>
          </div>
          <Badge variant="outline">{events.length} events</Badge>
        </div>
        <ol className="relative ml-4 border-l border-border pl-6">
          {events.map((event, index) => (
            <li key={event.id} className={index < events.length - 1 ? "pb-6" : undefined}>
              <span className="absolute -left-[17px] flex size-8 items-center justify-center rounded-full border border-border bg-background text-muted-foreground">
                <AuditIcon event={event} />
              </span>
              <article className="rounded-lg border border-border bg-card p-4">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <h3 className="font-medium">{event.action}</h3>
                    <p className="mt-0.5 text-sm text-muted-foreground">
                      {event.actor} · {event.role}
                    </p>
                  </div>
                  <time className="text-sm text-muted-foreground" dateTime={event.timestamp}>
                    {formatDate(event.timestamp)}
                  </time>
                </div>
                <p className="mt-3 text-ui">{event.object}</p>
                {(event.priorValue || event.newValue) && (
                  <dl className="mt-3 grid gap-2 rounded-md bg-muted/40 p-3 text-sm sm:grid-cols-2">
                    {event.priorValue && (
                      <div>
                        <dt className="text-muted-foreground">Prior</dt>
                        <dd className="mt-0.5">{event.priorValue}</dd>
                      </div>
                    )}
                    {event.newValue && (
                      <div>
                        <dt className="text-muted-foreground">New</dt>
                        <dd className="mt-0.5">{event.newValue}</dd>
                      </div>
                    )}
                  </dl>
                )}
              </article>
            </li>
          ))}
        </ol>
      </section>

      <aside className="space-y-5">
        <section className="rounded-lg border border-border bg-card p-4">
          <div className="mb-3 flex items-center gap-2">
            <HistoryIcon className="size-4 text-muted-foreground" />
            <h2 className="font-semibold">Version record</h2>
          </div>
          <dl className="grid gap-3 text-ui">
            <VersionLine label="Processing model" value={`v${caseData.processingModel.version}`} />
            <VersionLine label="Policy pack" value={caseData.policyPack.version} />
            <VersionLine label="Snapshot" value={caseData.snapshotLabel} />
            <VersionLine label="Jurisdiction" value="United Kingdom" />
          </dl>
        </section>

        <section className="rounded-lg border border-border bg-card p-4">
          <div className="mb-3 flex items-center gap-2">
            <ShieldCheckIcon className="size-4 text-status-green" />
            <h2 className="font-semibold">Verification record</h2>
          </div>
          <Badge
            variant="outline"
            className="mb-3 border-status-green/25 bg-status-green/10 text-status-green"
          >
            {caseData.verification.verdict.replaceAll("-", " ")}
          </Badge>
          <p className="text-ui text-muted-foreground">{caseData.verification.citationCoverage}</p>
          <dl className="mt-4 grid gap-3 border-t border-border pt-3 text-sm">
            <VersionLine label="Reviewer" value={caseData.verification.reviewer} />
            <VersionLine
              label="Blind review completed"
              value={formatDate(caseData.verification.blindedUntil)}
            />
            <VersionLine
              label="Recommendation review"
              value={formatDate(caseData.verification.reviewedAt)}
            />
          </dl>
        </section>

        <section className="rounded-lg border border-border bg-muted/30 p-4">
          <h2 className="font-semibold">Demo system-of-record boundary</h2>
          <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
            This case persists in versioned browser storage for the demonstration. It is not an
            immutable compliance record and must not contain real personal data.
          </p>
        </section>
      </aside>
    </div>
  );
}

function AuditIcon({ event }: { event: AuditEvent }) {
  if (event.role === "Privacy Officer") return <UserRoundIcon className="size-4" />;
  if (event.role === "System") return <BotIcon className="size-4" />;
  if (event.action.includes("Completed") || event.action.includes("Validated")) {
    return <CheckCircle2Icon className="size-4" />;
  }
  return <FileClockIcon className="size-4" />;
}

function VersionLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-medium">{value}</dd>
    </div>
  );
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}
