// "How this project works": every rule the project's agents follow, in one place.
// The owner (and, for the Mandate, product approvers) edits through the existing
// dialogs; everyone else reads. Nothing here changes who may edit what.

import { EyeIcon, PencilIcon, Settings2Icon } from "lucide-react";
import { type ReactNode, useState } from "react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
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

function RulesSection({
  title,
  action,
  children,
  testId,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  testId: string;
}) {
  return (
    <section aria-label={title} className="space-y-3" data-testid={testId}>
      <div className="flex items-center gap-2">
        <h2 className="min-w-0 flex-1 text-[length:calc(var(--text-ui)+2px)] font-bold">{title}</h2>
        {action}
      </div>
      {children}
    </section>
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
        <div className="max-h-96 overflow-y-auto rounded-md border border-border px-4 py-3">
          <TicketMarkdown className="text-sm leading-relaxed">{mandate}</TicketMarkdown>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">No mandate yet.</p>
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
      <ol className="space-y-2">
        {(stages.data?.stages ?? []).map((stage, index) => (
          <li
            key={stage.id}
            className="rounded-md border border-border px-3 py-2"
            data-testid="project-rules-stage"
          >
            <p className="text-sm font-medium">
              {index + 1}. {stage.name}
              {stage.is_complete && (
                <span className="ml-2 text-xs font-normal text-muted-foreground">Complete</span>
              )}
            </p>
            {stage.instructions.trim() ? (
              <TicketMarkdown className="mt-1 text-sm text-muted-foreground">
                {stage.instructions}
              </TicketMarkdown>
            ) : (
              <p className="mt-1 text-sm text-muted-foreground">No instructions.</p>
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
    <RulesSection title="Who approves what" testId="project-rules-approvals">
      <p className="text-sm text-muted-foreground">
        A change lands only once it has every sign-off it needs. People add these as labels on the
        ticket; agents can never add them.
      </p>
      <dl className="divide-y divide-border rounded-md border border-border">
        {SIGN_OFFS.map((signOff) => (
          <div
            key={signOff.label}
            className="grid gap-1 px-3 py-2 sm:grid-cols-[11rem_1fr]"
            data-testid="project-rules-sign-off"
          >
            <dt className="flex flex-col text-sm font-medium">
              {signOff.name}
              <code className="text-xs font-normal text-muted-foreground">{signOff.label}</code>
            </dt>
            <dd className="text-sm">
              <span>{signOff.when}</span>{" "}
              <span className="text-muted-foreground">{signOff.who}</span>
            </dd>
          </div>
        ))}
      </dl>
      <ProjectProductApproversSection projectId={projectId} />
    </RulesSection>
  );
}

function AdvancedRules({ projectName, projectId }: { projectName: string; projectId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <RulesSection title="Advanced" testId="project-rules-advanced">
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
  return (
    <div className="mx-auto w-full max-w-3xl space-y-8 px-1 pb-10" data-testid="project-rules">
      <header className="space-y-1">
        <h1 className="text-[length:calc(var(--text-ui)+6px)] font-bold">How this project works</h1>
        <p className="text-sm text-muted-foreground">
          The rules every agent here follows, and who can change them.
        </p>
      </header>
      {mandateEnabled && <MandateRules projectName={projectName} projectId={projectId} />}
      {stagesEnabled && <StageRules projectName={projectName} projectId={projectId} />}
      <ApprovalRules projectId={projectId} />
      <RulesSection title="People" testId="project-rules-people">
        <ProjectSharingSection projectId={projectId} />
      </RulesSection>
      <AdvancedRules projectName={projectName} projectId={projectId} />
    </div>
  );
}
