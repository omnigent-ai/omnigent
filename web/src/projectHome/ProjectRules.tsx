// "How this project works": every rule the project's agents follow, in one place.
// The owner (and, for the Mandate, product approvers) edits through the existing
// dialogs; everyone else reads. Nothing here changes who may edit what.

import { EyeIcon, PencilIcon, Settings2Icon } from "lucide-react";
import { type MouseEvent, type ReactNode, useState } from "react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { cn } from "@/lib/utils";
import { ProjectSettingsDialog } from "@/shell/ProjectSettingsDialog";
import { ProjectProductApproversSection, ProjectSharingSection } from "@/shell/ProjectSharing";
import { canEditMandate, ProjectMandateDialog, useProjectMandate } from "./ProjectMandateDialog";
import { ProjectTicketStagesDialog } from "./ProjectTicketStagesDialog";
import { TicketLoadError } from "./TicketControls";
import { TicketMarkdown } from "./ticketImages";
import { useTicketProject, useTicketStages, useTicketStagesEnabled } from "./useProjectTickets";

/** What each sign-off label means, in the owner's and a teammate's terms. */
const SIGN_OFFS: { label: string; name: string; when: string; who: string }[] = [
  {
    label: "approved",
    name: "Review",
    when: "Every code change, before it lands.",
    who: "The Reviewer, for the exact commit it checked. Any new commit clears it.",
  },
  {
    label: "product-ok",
    name: "Product approval",
    when: "A new feature someone other than the owner asked for.",
    who: "The owner or a product approver, themselves.",
  },
  {
    label: "risk-ok",
    name: "Risk approval",
    when: "Changes to permissions, database migrations, data deletion or these rules.",
    who: "The owner, or a product approver who didn't ask for that change.",
  },
  {
    label: "owner-ok",
    name: "Owner approval",
    when: "A pull request from outside the project.",
    who: "The owner only, for the approved commit.",
  },
];

/** Page sections, in reading order; ids double as in-page anchors. */
type SectionKey = "mandate" | "stages" | "approvals" | "people" | "advanced";
const SECTION_LABELS: Record<SectionKey, string> = {
  mandate: "Mandate",
  stages: "Stages",
  approvals: "Who approves what",
  people: "People",
  advanced: "Advanced",
};
const sectionAnchor = (key: SectionKey) => `project-rules-section-${key}`;

function RulesSection({
  title,
  section,
  action,
  children,
  testId,
}: {
  title: string;
  section: SectionKey;
  action?: ReactNode;
  children: ReactNode;
  testId: string;
}) {
  return (
    <section
      id={sectionAnchor(section)}
      aria-label={title}
      className="scroll-mt-14 space-y-3 border-t border-border pt-6 first:border-t-0 first:pt-0"
      data-testid={testId}
    >
      <div className="flex min-h-8 items-center gap-2">
        <h2 className="min-w-0 flex-1 text-[length:calc(var(--text-ui)+2px)] font-semibold">
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}

/** Section anchors: a sticky side list on desktop, a scrolling chip row on phones. */
function RulesNav({ sections }: { sections: SectionKey[] }) {
  const jump = (event: MouseEvent<HTMLAnchorElement>, key: SectionKey) => {
    // Scroll in place; the router owns the URL, so leave the hash alone.
    event.preventDefault();
    document.getElementById(sectionAnchor(key))?.scrollIntoView({ block: "start" });
  };
  return (
    <nav
      aria-label="Rules sections"
      className={cn(
        "sticky top-0 z-10 -mx-1 bg-background/95 px-1 py-2 backdrop-blur",
        "lg:top-4 lg:mx-0 lg:self-start lg:bg-transparent lg:p-0 lg:backdrop-blur-none",
      )}
      data-testid="project-rules-nav"
    >
      <ul className="flex gap-1.5 overflow-x-auto lg:flex-col lg:gap-0.5 lg:overflow-visible">
        {sections.map((key) => (
          <li key={key} className="shrink-0">
            <a
              href={`#${sectionAnchor(key)}`}
              onClick={(event) => jump(event, key)}
              className={cn(
                "block rounded-full border border-border px-3 py-1 text-xs whitespace-nowrap text-muted-foreground",
                "hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
                "lg:rounded-md lg:border-0 lg:px-2 lg:py-1.5 lg:text-sm",
              )}
            >
              {SECTION_LABELS[key]}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function MandateRules({ projectName, projectId }: { projectName: string; projectId: string }) {
  const project = useProjectMandate(projectId);
  const viewerId = useViewerId();
  const [open, setOpen] = useState(false);
  const canEdit = canEditMandate(project.data, viewerId);
  const mandate = (project.data?.mandate ?? "").trim();
  const pending = project.data?.mandate_proposals?.length ?? 0;
  return (
    <RulesSection
      title="Mandate"
      section="mandate"
      testId="project-rules-mandate"
      action={
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setOpen(true)}
          data-testid="project-rules-mandate-open"
        >
          {canEdit ? <PencilIcon aria-hidden /> : <EyeIcon aria-hidden />}
          {canEdit ? (mandate ? "Edit mandate" : "Write mandate") : "View"}
        </Button>
      }
    >
      <p className="text-sm text-muted-foreground">
        Bounds every agent in the project follows, including the Lead and its workers.{" "}
        {canEdit
          ? "You can change it."
          : "Only the owner or a product approver can change it; agents can only propose changes."}
      </p>
      {pending > 0 && (
        <p className="text-sm font-medium" data-testid="project-rules-mandate-pending">
          {pending === 1 ? "1 proposed change" : `${pending} proposed changes`} waiting
          {canEdit ? " for you" : " for the owner or a product approver"}.
        </p>
      )}
      {project.isPending ? (
        <Spinner aria-label="Loading mandate" />
      ) : mandate ? (
        <div className="max-h-96 overflow-y-auto rounded-md bg-muted/50 px-4 py-3">
          <TicketMarkdown className="text-sm leading-relaxed">{mandate}</TicketMarkdown>
        </div>
      ) : (
        <p className="rounded-md bg-muted/50 px-4 py-3 text-sm text-muted-foreground">
          No mandate yet.
        </p>
      )}
      {open && <ProjectMandateDialog projectName={projectName} onClose={() => setOpen(false)} />}
    </RulesSection>
  );
}

function StageRules({ projectName, projectId }: { projectName: string; projectId: string }) {
  const stages = useTicketStages(projectId);
  const [open, setOpen] = useState(false);
  return (
    <RulesSection
      title="Stages"
      section="stages"
      testId="project-rules-stages"
      action={
        stages.supported && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setOpen(true)}
            data-testid="project-rules-stages-edit"
          >
            <PencilIcon aria-hidden />
            Edit stages
          </Button>
        )
      }
    >
      <p className="text-sm text-muted-foreground">
        The board's columns, in order. Tickets start in the first; the last means done. Agents
        follow each stage's instructions.
      </p>
      <ol className="divide-y divide-border">
        {(stages.data?.stages ?? []).map((stage, index) => (
          <li
            key={stage.id}
            className="grid grid-cols-[2rem_1fr] gap-x-2 py-2.5 sm:grid-cols-[2rem_9rem_1fr_auto]"
            data-testid="project-rules-stage"
          >
            <span
              aria-hidden
              className="pt-px text-xs text-muted-foreground tabular-nums"
            >
              {String(index + 1).padStart(2, "0")}
            </span>
            <p className="flex items-center gap-2 text-sm font-semibold">
              <span className="sr-only">{index + 1}. </span>
              {stage.name}
              {stage.is_complete && (
                <span className="rounded-full border border-border px-2 py-px text-xs font-normal text-muted-foreground sm:hidden">
                  Complete
                </span>
              )}
            </p>
            <div className="col-start-2 min-w-0 sm:col-start-3">
              {stage.instructions.trim() ? (
                <TicketMarkdown className="text-sm text-muted-foreground">
                  {stage.instructions}
                </TicketMarkdown>
              ) : (
                <p className="text-sm text-muted-foreground">No instructions</p>
              )}
            </div>
            {stage.is_complete && (
              <span
                aria-hidden
                className="hidden self-start rounded-full border border-border px-2 py-px text-xs text-muted-foreground sm:block"
              >
                Complete
              </span>
            )}
          </li>
        ))}
      </ol>
      {open && (
        <ProjectTicketStagesDialog projectName={projectName} onClose={() => setOpen(false)} />
      )}
    </RulesSection>
  );
}

function ApprovalRules({ projectId }: { projectId: string }) {
  return (
    <RulesSection title="Who approves what" section="approvals" testId="project-rules-approvals">
      <p className="text-sm text-muted-foreground">
        A change lands only once it has every sign-off it needs. The Reviewer gives the review
        approval; only people can give the others, never agents.
      </p>
      <dl className="divide-y divide-border">
        {SIGN_OFFS.map((signOff) => (
          <div key={signOff.label} className="space-y-0.5 py-3" data-testid="project-rules-sign-off">
            <dt className="text-sm font-semibold">{signOff.name}</dt>
            <dd className="text-sm">{signOff.when}</dd>
            <dd className="text-sm text-muted-foreground">{signOff.who}</dd>
          </div>
        ))}
      </dl>
      <details className="text-sm" data-testid="project-rules-label-details">
        <summary className="cursor-pointer rounded-sm text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
          Ticket labels
        </summary>
        <p className="mt-2 text-muted-foreground">Each sign-off is added as a label on its ticket.</p>
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
          {SIGN_OFFS.map((signOff) => (
            <div key={signOff.label} className="contents" data-testid="project-rules-sign-off-label">
              <dt className="text-muted-foreground">{signOff.name}</dt>
              <dd>
                <code className="font-mono text-xs">{signOff.label}</code>
              </dd>
            </div>
          ))}
        </dl>
      </details>
      <ProjectProductApproversSection projectId={projectId} />
    </RulesSection>
  );
}

function AdvancedRules({ projectName, projectId }: { projectName: string; projectId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <RulesSection title="Advanced" section="advanced" testId="project-rules-advanced">
      <p className="text-sm text-muted-foreground">
        Where new sessions run by default: host, working directory, worktree and agent.
      </p>
      <div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => setOpen(true)}
          data-testid="project-rules-session-defaults"
        >
          <Settings2Icon aria-hidden />
          Session defaults…
        </Button>
      </div>
      <ProjectSettingsDialog
        open={open}
        onOpenChange={setOpen}
        projectId={projectId}
        projectName={projectName}
        defaultsOnly
      />
    </RulesSection>
  );
}

/** The project's Rules tab: mandate, stages, sign-offs, people and session defaults. */
export function ProjectRules({ projectName }: { projectName: string }) {
  const { projectId, projects } = useTicketProject(projectName);
  const info = useServerInfo();
  const stagesEnabled = useTicketStagesEnabled();
  const mandateEnabled = info !== "loading" && info.project_mandate === true;
  if (projects.isLoading) return <Spinner aria-label="Loading project" />;
  if (projectId === null)
    return (
      <div className="mx-auto w-full max-w-5xl px-1 pt-3">
        <TicketLoadError error={projects.error ?? new Error("Project not found.")} />
      </div>
    );
  const sections: SectionKey[] = [
    ...(mandateEnabled ? (["mandate"] as const) : []),
    ...(stagesEnabled ? (["stages"] as const) : []),
    "approvals",
    "people",
    "advanced",
  ];
  return (
    <div className="mx-auto w-full max-w-5xl px-1 pt-2 pb-10 max-sm:px-4" data-testid="project-rules">
      <header className="space-y-1 pb-4 lg:pb-6">
        <h1 className="text-[length:calc(var(--text-ui)+8px)] font-bold tracking-tight">
          How this project works
        </h1>
        <p className="text-sm text-muted-foreground">
          The rules every agent here follows, and who can change them.
        </p>
      </header>
      <div className="lg:grid lg:grid-cols-[10rem_minmax(0,48rem)] lg:gap-10">
        <RulesNav sections={sections} />
        <div className="mt-4 space-y-8 lg:mt-0">
          {mandateEnabled && <MandateRules projectName={projectName} projectId={projectId} />}
          {stagesEnabled && <StageRules projectName={projectName} projectId={projectId} />}
          <ApprovalRules projectId={projectId} />
          <RulesSection title="People" section="people" testId="project-rules-people">
            <ProjectSharingSection projectId={projectId} />
          </RulesSection>
          <AdvancedRules projectName={projectName} projectId={projectId} />
        </div>
      </div>
    </div>
  );
}
