// ⌘K's actions for the project in view, outside the project home: the open session's project,
// or the one a new session starts in. The project home offers its own.

import {
  ArrowUpDownIcon,
  MessageCirclePlusIcon,
  PencilIcon,
  Settings2Icon,
  Trash2Icon,
} from "lucide-react";
import { useMemo } from "react";

import { type Conversation, type ProjectSummary, useProjects } from "@/hooks/useConversations";
import { useOpenSessionRow } from "@/hooks/useOpenSessionRow";
import { useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isSingleUserMode } from "@/lib/capabilities";
import { useSearchParams } from "@/lib/routing";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import type { PaletteCommand } from "@/store/paletteCommands";

import { type CommandView, useCommandContext } from "../commandContext";
import { useQuickPickPaletteCommands } from "../screenCommands/useQuickPickPaletteCommands";
import { sessionOwnership } from "../sessionCommands/model";
import { projectMoveCommands } from "./ordering";
import { type ProjectCommandId, requestProjectAction } from "./projectActionStore";
import { type ProjectOrdering, useProjectOrdering } from "./projectOrderingStore";

const NO_COMMANDS: PaletteCommand[] = [];

/** Where a new session filed under `projectName` starts. */
export function newSessionInProjectPath(projectName: string): string {
  return `/?project=${encodeURIComponent(projectName)}`;
}

/**
 * The project the user is looking at, from the caller's own project list. None while the list
 * loads, and none for a session filed in a project the caller doesn't have, such as a shared one.
 * A project label names a folder only in its owner's list, so only the viewer's own session
 * is matched by it.
 */
export function projectInView({
  view,
  session,
  ownSession,
  newSessionProject,
  projects,
}: {
  view: CommandView;
  session: Pick<Conversation, "project_id" | "labels"> | null;
  /** The viewer owns the open session, as its header's owner-only actions decide. */
  ownSession: boolean;
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
  const label = ownSession ? session.labels?.[PROJECT_LABEL_KEY] : undefined;
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

/**
 * The project in view's folder actions under "Project", each naming the project, then the
 * folder menu's moves while the sidebar lists it.
 */
export function projectPaletteCommands(
  project: ProjectSummary,
  view: CommandView,
  ordering: ProjectOrdering | null = null,
): PaletteCommand[] {
  const index = ordering ? ordering.names.indexOf(project.name) : -1;
  const moves: PaletteCommand[] =
    ordering && index >= 0
      ? projectMoveCommands(project.name, index, ordering.names.length, ordering.facts).map(
          (move) => ({
            id: `project.move-${move.destination}:${project.name}`,
            label: move.label,
            icon: ArrowUpDownIcon,
            keywords: [...move.keywords],
            availability: move.availability,
            run: () => ordering.move(project.name, move.destination),
          }),
        )
      : [];
  return [
    ...COMMANDS
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
      })),
    ...moves,
  ];
}

export function useProjectPaletteCommands(): void {
  const { view, session } = useCommandContext();
  const sessionRow = useOpenSessionRow(view === "session" ? (session?.id ?? null) : null);
  const [searchParams] = useSearchParams();
  const { data: projects } = useProjects();
  const viewerId = useViewerId();
  const serverInfo = useServerInfo();
  const project = projectInView({
    view,
    session: sessionRow,
    ownSession:
      sessionRow !== null &&
      sessionOwnership(sessionRow, viewerId, isSingleUserMode(serverInfo)) === "mine",
    newSessionProject: searchParams.get("project"),
    projects,
  });
  const ordering = useProjectOrdering();
  const commands = useMemo(
    () => (project ? projectPaletteCommands(project, view, ordering) : NO_COMMANDS),
    [project, view, ordering],
  );
  useQuickPickPaletteCommands("project-in-view", "Project", commands);
}
