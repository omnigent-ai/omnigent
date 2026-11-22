// ⌘K's actions for the project in view, outside the project home: the open session's project,
// or the one a new session starts in. The project home offers its own.

import { MessageCirclePlusIcon, PencilIcon, Settings2Icon, Trash2Icon } from "lucide-react";
import { useMemo } from "react";

import { type Conversation, type ProjectSummary, useProjects } from "@/hooks/useConversations";
import { useOpenSessionRow } from "@/hooks/useOpenSessionRow";
import { useSearchParams } from "@/lib/routing";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import type { PaletteCommand } from "@/store/paletteCommands";

import { type CommandView, useCommandContext } from "../commandContext";
import { useQuickPickPaletteCommands } from "../screenCommands/useQuickPickPaletteCommands";
import { type ProjectCommandId, requestProjectAction } from "./projectActionStore";

const NO_COMMANDS: PaletteCommand[] = [];

/** Where a new session filed under `projectName` starts. */
export function newSessionInProjectPath(projectName: string): string {
  return `/?project=${encodeURIComponent(projectName)}`;
}

/**
 * The project the user is looking at, from the caller's own project list. None while the list
 * loads, and none for a session filed in a project the caller doesn't have, such as a shared one.
 */
export function projectInView({
  view,
  session,
  newSessionProject,
  projects,
}: {
  view: CommandView;
  session: Pick<Conversation, "project_id" | "labels"> | null;
  newSessionProject: string | null;
  projects: readonly ProjectSummary[] | undefined;
}): ProjectSummary | null {
  if (!projects) return null;
  if (view === "new-session") {
    return projects.find((project) => project.name === newSessionProject) ?? null;
  }
  if (view !== "session" || !session) return null;
  if (session.project_id != null) {
    return projects.find((project) => project.id === session.project_id) ?? null;
  }
  const label = session.labels?.[PROJECT_LABEL_KEY];
  return label ? (projects.find((project) => project.name === label) ?? null) : null;
}

const COMMANDS: readonly {
  id: ProjectCommandId;
  label: (name: string) => string;
  icon: PaletteCommand["icon"];
  keywords: readonly string[];
}[] = [
  {
    id: "new-session",
    label: (name) => `New session in ${name}`,
    icon: MessageCirclePlusIcon,
    keywords: ["chat", "start"],
  },
  {
    id: "settings",
    label: (name) => `Project settings for ${name}…`,
    icon: Settings2Icon,
    keywords: ["defaults", "host", "agent", "workspace"],
  },
  {
    id: "rename",
    label: (name) => `Rename project ${name}…`,
    icon: PencilIcon,
    keywords: ["icon", "emoji"],
  },
  {
    id: "delete",
    label: (name) => `Delete project ${name}…`,
    icon: Trash2Icon,
    keywords: ["remove", "archive"],
  },
];

/** The project in view's folder actions under "Project", each naming the project. */
export function projectPaletteCommands(
  project: ProjectSummary,
  view: CommandView,
): PaletteCommand[] {
  return (
    COMMANDS
      // The new-session view already starts a session in its project.
      .filter((command) => !(view === "new-session" && command.id === "new-session"))
      .map((command) => ({
        id: `project.${command.id}:${project.name}`,
        label: command.label(project.name),
        icon: command.icon,
        keywords: ["project", "folder", project.name, ...command.keywords],
        // A dialog or the new session's composer takes focus once the palette has closed.
        movesFocus: true,
        run: () => requestProjectAction(project, command.id),
      }))
  );
}

export function useProjectPaletteCommands(): void {
  const { view, session } = useCommandContext();
  const sessionRow = useOpenSessionRow(view === "session" ? (session?.id ?? null) : null);
  const [searchParams] = useSearchParams();
  const { data: projects } = useProjects();
  const project = projectInView({
    view,
    session: sessionRow,
    newSessionProject: searchParams.get("project"),
    projects,
  });
  const commands = useMemo(
    () => (project ? projectPaletteCommands(project, view) : NO_COMMANDS),
    [project, view],
  );
  useQuickPickPaletteCommands("project-in-view", "Project", commands);
}
