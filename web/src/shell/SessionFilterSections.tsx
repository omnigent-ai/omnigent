// The sidebar filter menu's sections: pull request, status, harness and project, one choice
// each, and Clear filters while any is on.

import type { ReactNode } from "react";

import {
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import {
  SESSION_PULL_REQUEST_FILTER_LABELS,
  SESSION_PULL_REQUEST_FILTERS,
  SESSION_STATUS_FILTER_LABELS,
  SESSION_STATUS_FILTERS,
  type SessionListFilter,
  type SessionProjectFilter,
  sessionListFilterCount,
} from "@/lib/sessionListFilters";

/** What the menu's Filter entry says beside its label: how many filters are on, if any. */
export function sessionListFilterSummary(filter: SessionListFilter): string | null {
  const count = sessionListFilterCount(filter);
  return count === 0 ? null : `${count} on`;
}

const projectChoiceValue = (project: SessionProjectFilter) =>
  project.kind === "project" ? `project:${project.id}` : project.kind;

function projectFilterFromValue(value: string): SessionProjectFilter {
  if (value === "none") return { kind: "none" };
  if (value.startsWith("project:")) return { kind: "project", id: value.slice("project:".length) };
  return { kind: "all" };
}

function Section({
  label,
  value,
  onValueChange,
  children,
}: {
  label: string;
  value: string;
  onValueChange: (value: string) => void;
  children: ReactNode;
}) {
  return (
    <>
      <DropdownMenuLabel className="text-muted-foreground text-sm">{label}</DropdownMenuLabel>
      <DropdownMenuRadioGroup value={value} onValueChange={onValueChange}>
        {children}
      </DropdownMenuRadioGroup>
    </>
  );
}

function Choice({
  value,
  testId,
  children,
}: {
  value: string;
  testId: string;
  children: ReactNode;
}) {
  return (
    <DropdownMenuRadioItem className="mobile-touch-target" value={value} data-testid={testId}>
      <span className="truncate">{children}</span>
    </DropdownMenuRadioItem>
  );
}

export function SessionFilterSections({
  value,
  onChange,
  onClear,
  harnesses,
  projects,
}: {
  value: SessionListFilter;
  onChange: (patch: Partial<SessionListFilter>) => void;
  onClear: () => void;
  /** The harnesses to offer, by name, as `sessionHarnessChoices` gives them. */
  harnesses: readonly { key: string; name: string }[];
  /** The viewer's projects. */
  projects: readonly { id: string; name: string }[];
}) {
  const chosenProject = value.project;
  const missingProject =
    chosenProject.kind === "project" &&
    !projects.some((project) => project.id === chosenProject.id);
  return (
    <>
      <Section
        label="Pull request"
        value={value.pullRequest}
        onValueChange={(next) => {
          const pullRequest = SESSION_PULL_REQUEST_FILTERS.find((choice) => choice === next);
          if (pullRequest) onChange({ pullRequest });
        }}
      >
        {SESSION_PULL_REQUEST_FILTERS.map((filter) => (
          <Choice key={filter} value={filter} testId={`session-pull-request-filter-${filter}`}>
            {SESSION_PULL_REQUEST_FILTER_LABELS[filter]}
          </Choice>
        ))}
      </Section>
      <DropdownMenuSeparator />
      <Section
        label="Status"
        value={value.status}
        onValueChange={(next) => {
          const status = SESSION_STATUS_FILTERS.find((choice) => choice === next);
          if (status) onChange({ status });
        }}
      >
        {SESSION_STATUS_FILTERS.map((filter) => (
          <Choice key={filter} value={filter} testId={`session-status-filter-${filter}`}>
            {SESSION_STATUS_FILTER_LABELS[filter]}
          </Choice>
        ))}
      </Section>
      <DropdownMenuSeparator />
      <Section
        label="Harness"
        value={value.harness === null ? "all" : `harness:${value.harness}`}
        onValueChange={(next) =>
          onChange({ harness: next.startsWith("harness:") ? next.slice("harness:".length) : null })
        }
      >
        <Choice value="all" testId="session-harness-filter-all">
          Any harness
        </Choice>
        {harnesses.map((harness) => (
          <Choice
            key={harness.key}
            value={`harness:${harness.key}`}
            testId="session-harness-filter"
          >
            {harness.name}
          </Choice>
        ))}
      </Section>
      <DropdownMenuSeparator />
      <Section
        label="Project"
        value={projectChoiceValue(chosenProject)}
        onValueChange={(next) => onChange({ project: projectFilterFromValue(next) })}
      >
        <Choice value="all" testId="session-project-filter-all">
          Any project
        </Choice>
        <Choice value="none" testId="session-project-filter-none">
          No project
        </Choice>
        {projects.map((project) => (
          <Choice key={project.id} value={`project:${project.id}`} testId="session-project-filter">
            {project.name}
          </Choice>
        ))}
        {missingProject && (
          <Choice value={projectChoiceValue(chosenProject)} testId="session-project-filter-missing">
            Missing project
          </Choice>
        )}
      </Section>
      {sessionListFilterCount(value) > 0 && (
        <>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            className="mobile-touch-target"
            data-testid="session-filters-clear"
            onSelect={onClear}
          >
            Clear filters
          </DropdownMenuItem>
        </>
      )}
    </>
  );
}
