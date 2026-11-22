// Carries out a project's folder action requested away from its folder, with the folder
// menu's own dialogs, and offers the project in view's actions in ⌘K.

import { useEffect } from "react";

import { useNavigate, useSearchParams } from "@/lib/routing";

import { useProjectFolderMenu } from "../Sidebar";
import {
  type ProjectActionRequest,
  reportProjectRenamed,
  useProjectActionStore,
} from "./projectActionStore";
import { newSessionInProjectPath, useProjectPaletteCommands } from "./useProjectPaletteCommands";

/** Mount once, inside the shell's providers. */
export function ProjectActionHost() {
  useProjectPaletteCommands();
  const request = useProjectActionStore((state) => state.request);
  // A request left over from an earlier shell has nothing to come back to.
  useEffect(() => () => useProjectActionStore.setState({ request: null }), []);
  return request ? <ProjectActionRunner key={request.key} request={request} /> : null;
}

function ProjectActionRunner({ request }: { request: ProjectActionRequest }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { project, command } = request;
  // The new-session view filed under this project follows it to its new name, or leaves it.
  const composingInProject = searchParams.get("project") === project.name;
  const { actions, dialogs } = useProjectFolderMenu(
    project.name,
    project.id,
    project.icon,
    (oldName, newName) => {
      reportProjectRenamed(oldName, newName);
      if (composingInProject) navigate(newSessionInProjectPath(newName), { replace: true });
    },
    () => {
      if (composingInProject) navigate("/", { replace: true });
    },
  );
  useEffect(() => {
    switch (command) {
      case "new-session":
        navigate(newSessionInProjectPath(project.name));
        return;
      case "rename":
        actions.openRename();
        return;
      case "settings":
        actions.openSettings();
        return;
      case "delete":
        actions.openDelete();
        return;
    }
    // Runs once per request; the runner remounts for the next one.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return dialogs;
}
