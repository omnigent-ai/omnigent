import type { ReactNode } from "react";
import { ListFilterIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import type { Project } from "@/lib/projectsApi";
import { newView, type ViewInput } from "@/lib/workViewsApi";
import { PRIORITIES, SESSION_STATUSES, TASK_STATUSES } from "./viewPresentation";

interface FilterGroup {
  key: string;
  label: string;
  summary: string | null;
  clear?: () => void;
  editor: ReactNode;
}

function FilterOptions({
  options,
  selected,
  onChange,
}: {
  options: Record<string, string>;
  selected: string[];
  onChange: (selected: string[]) => void;
}) {
  return (
    <div className="flex max-h-48 flex-wrap gap-1.5 overflow-y-auto">
      {Object.entries(options).map(([key, label]) => (
        <Button
          key={key}
          size="sm"
          variant={selected.includes(key) ? "secondary" : "outline"}
          aria-pressed={selected.includes(key)}
          onClick={() =>
            onChange(
              selected.includes(key) ? selected.filter((item) => item !== key) : [...selected, key],
            )
          }
        >
          {label}
        </Button>
      ))}
    </div>
  );
}

function FilterField({ group }: { group: FilterGroup }) {
  return (
    <fieldset className="space-y-2">
      <legend className="mb-2 text-sm text-muted-foreground">{group.label}</legend>
      {group.editor}
    </fieldset>
  );
}

export function ViewFilters({
  value,
  projects,
  onChange,
}: {
  value: ViewInput;
  projects: Pick<Project, "id" | "name">[];
  onChange: (view: ViewInput) => void;
}) {
  const filters = value.filters;
  const setFilters = (changes: Partial<typeof filters>) =>
    onChange({ ...value, filters: { ...filters, ...changes } });
  const projectNames = Object.fromEntries(projects.map((project) => [project.id, project.name]));
  const selectedProjects = [...filters.project_ids, ...(filters.no_project ? ["none"] : [])];
  const projectOptions: Record<string, string> = {
    ...Object.fromEntries(filters.project_ids.map((id) => [id, "Unavailable project"])),
    ...projectNames,
    ...(value.entity_type !== "projects" ? { none: "No project" } : {}),
  };
  const statuses: Record<string, string> =
    value.entity_type === "tasks" ? TASK_STATUSES : SESSION_STATUSES;
  const updatedDate =
    filters.updated_after === null
      ? ""
      : new Date(filters.updated_after * 1000).toISOString().slice(0, 10);
  const groups: FilterGroup[] = [
    {
      key: "search",
      label: "Search",
      summary: filters.search.trim() || null,
      clear: () => setFilters({ search: "" }),
      editor: (
        <Input
          aria-label="Search filter"
          value={filters.search}
          maxLength={200}
          onChange={(event) => setFilters({ search: event.target.value })}
        />
      ),
    },
    ...(value.entity_type !== "projects"
      ? [
          {
            key: "status",
            label: "Status",
            summary: filters.statuses.length
              ? filters.statuses.map((status) => statuses[status]).join(", ")
              : null,
            clear: () => setFilters({ statuses: [] }),
            editor: (
              <FilterOptions
                options={statuses}
                selected={filters.statuses}
                onChange={(selected) =>
                  setFilters({
                    statuses: selected,
                    ...(selected.includes("archived") ? { include_archived: true } : {}),
                  })
                }
              />
            ),
          },
        ]
      : []),
    {
      key: "project",
      label: "Project",
      summary: selectedProjects.length
        ? selectedProjects.map((id) => projectOptions[id]).join(", ")
        : null,
      clear: () => setFilters({ project_ids: [], no_project: false }),
      editor: (
        <FilterOptions
          options={projectOptions}
          selected={selectedProjects}
          onChange={(selected) =>
            setFilters({
              project_ids: selected.filter((id) => id !== "none"),
              no_project: selected.includes("none"),
            })
          }
        />
      ),
    },
    ...(value.entity_type === "tasks"
      ? [
          {
            key: "priority",
            label: "Priority",
            summary: filters.priorities.length
              ? filters.priorities.map((priority) => PRIORITIES[priority]).join(", ")
              : null,
            clear: () => setFilters({ priorities: [] }),
            editor: (
              <FilterOptions
                options={PRIORITIES}
                selected={filters.priorities.map(String)}
                onChange={(selected) => setFilters({ priorities: selected.map(Number) })}
              />
            ),
          },
        ]
      : []),
    {
      key: "updated",
      label: "Updated since",
      summary: updatedDate || null,
      clear: () => setFilters({ updated_after: null }),
      editor: (
        <Input
          type="date"
          aria-label="Updated since"
          value={updatedDate}
          onChange={(event) =>
            setFilters({
              updated_after: event.target.value
                ? Date.parse(`${event.target.value}T00:00:00Z`) / 1000
                : null,
            })
          }
        />
      ),
    },
    ...(value.entity_type === "sessions"
      ? [
          {
            key: "archived",
            label: "Archived",
            summary: filters.include_archived ? "Included" : "Excluded",
            clear: filters.include_archived
              ? () =>
                  setFilters({
                    include_archived: false,
                    statuses: filters.statuses.filter((status) => status !== "archived"),
                  })
              : undefined,
            editor: (
              <label className="flex items-center justify-between text-sm">
                Include archived
                <Switch
                  checked={filters.include_archived}
                  onCheckedChange={(checked) =>
                    setFilters({
                      include_archived: checked,
                      statuses: checked
                        ? filters.statuses
                        : filters.statuses.filter((status) => status !== "archived"),
                    })
                  }
                />
              </label>
            ),
          },
        ]
      : []),
  ];
  const activeGroups = groups.filter((group) => group.summary !== null);
  const filterCount = activeGroups.filter((group) => group.clear).length;
  return (
    <>
      <div
        className="flex min-w-0 flex-wrap items-center gap-2"
        role="group"
        aria-label="Applied filters"
      >
        {activeGroups.map((group) => (
          <div
            key={group.key}
            className="inline-flex max-w-full items-center rounded-full border border-border bg-background"
          >
            <Popover>
              <PopoverTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-auto min-h-8 min-w-0 rounded-full px-3 py-1 text-left whitespace-normal"
                  aria-label={`Edit ${group.label} filter: ${group.summary}`}
                >
                  <span className="break-words">
                    <span className="text-muted-foreground">{group.label}: </span>
                    {group.summary}
                  </span>
                </Button>
              </PopoverTrigger>
              <PopoverContent
                align="start"
                className="w-80 p-4"
                aria-label={`${group.label} filter`}
              >
                <FilterField group={group} />
              </PopoverContent>
            </Popover>
            {group.clear && (
              <Button
                variant="ghost"
                size="icon-xs"
                className="mr-1 shrink-0 rounded-full"
                aria-label={`Remove ${group.label} filter`}
                onClick={group.clear}
              >
                <XIcon />
              </Button>
            )}
          </div>
        ))}
        {activeGroups.length === 0 && (
          <span className="text-xs text-muted-foreground">No filters</span>
        )}
      </div>
      <Popover>
        <PopoverTrigger asChild>
          <Button
            variant="outline"
            size="icon"
            className="shrink-0 rounded-full"
            aria-label={`Filters${filterCount ? ` (${filterCount})` : ""}`}
          >
            <ListFilterIcon />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="max-h-[70vh] w-80 space-y-4 overflow-y-auto p-4">
          <div className="flex items-center justify-between">
            <span className="font-medium">Filters</span>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onChange({ ...value, filters: newView(value.entity_type).filters })}
            >
              Clear all
            </Button>
          </div>
          {groups.map((group) => (
            <FilterField key={group.key} group={group} />
          ))}
        </PopoverContent>
      </Popover>
    </>
  );
}
