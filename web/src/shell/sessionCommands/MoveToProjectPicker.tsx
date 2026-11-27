// The keyboard way to file a session into a project, as the row menu's project list does:
// pick a project, "Remove from …" when it's filed, or "Create …" for a new name.

import { FolderMinusIcon, PlusIcon } from "lucide-react";
import { useState } from "react";

import { QuickPickDialog, type QuickPickItem } from "@/components/ui/quick-pick";
import { useProjects } from "@/hooks/useConversations";

import { canCreateProject } from "../projectChoices";
import { ProjectRowIcon } from "../ProjectPicker";

/** Accepting files the session under `project`; an empty name unfiles it. */
interface ProjectChoice {
  project: string;
}

export function MoveToProjectPicker({
  currentProject,
  onPick,
  onClose,
}: {
  currentProject: string | null;
  /** Called with the chosen project, or `""` to unfile; moving creates a new one on demand. */
  onPick: (project: string) => void;
  onClose: () => void;
}) {
  const { data: projects = [] } = useProjects();
  const [query, setQuery] = useState("");
  const items: QuickPickItem<ProjectChoice>[] = [
    ...(currentProject
      ? [
          {
            id: "remove",
            label: `Remove from ${currentProject}`,
            icon: <FolderMinusIcon aria-hidden className="size-4 text-muted-foreground" />,
            rememberInHistory: false,
            value: { project: "" },
          },
        ]
      : []),
    ...projects
      .filter((project) => project.name !== currentProject)
      .map((project) => ({
        id: `project:${project.name}`,
        label: project.name,
        icon: <ProjectRowIcon icon={project.icon} className="size-4" />,
        value: { project: project.name },
      })),
  ];
  return (
    <QuickPickDialog<ProjectChoice>
      // Opened from ⌘K, so it steps with Ctrl-N/Ctrl-P as ⌘K does.
      stepsWithCtrlNP
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      label={currentProject ? "Move session to a project" : "Add session to a project"}
      placeholder="Find or create a project…"
      emptyMessage="No projects yet. Type a name to create one."
      acceptLabel="Move"
      query={query}
      onQueryChange={setQuery}
      segments={[{ sourceId: "projects", ranking: "client", items }]}
      createItem={(typed) => createProjectItem(typed, projects)}
      onAccept={({ value }) => {
        onPick(value.project);
        return undefined;
      }}
    />
  );
}

/** The "Create …" row for a typed name that isn't a project yet. */
function createProjectItem(
  query: string,
  projects: readonly { name: string }[],
): QuickPickItem<ProjectChoice> | null {
  if (!canCreateProject(projects, query)) return null;
  const name = query.trim();
  return {
    id: `create:${name}`,
    label: `Create ${name}`,
    icon: <PlusIcon aria-hidden className="size-4 text-muted-foreground" />,
    rememberInHistory: false,
    value: { project: name },
  };
}
