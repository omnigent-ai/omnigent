import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { getProject } from "@/lib/projectsApi";
import { ApiError, getSessionSlim } from "@/lib/sessionsApi";
import { useTicketProject } from "./useProjectTickets";

/** The saved binding, not the most recent project session. Navigation only reads it. */
export function useProjectAgentSession(projectId: string | null) {
  const project = useQuery({
    queryKey: ["project-record", projectId],
    queryFn: () => getProject(projectId!),
    enabled: projectId !== null,
    refetchInterval: 15_000,
  });
  const storedSessionId =
    project.data?.main_session_id !== undefined
      ? project.data.main_session_id
      : (project.data?.orchestrator_session_id ?? null);
  const state = project.data?.main_session_state ?? (storedSessionId ? "active" : "unavailable");
  return {
    ...project,
    storedSessionId,
    state,
    sessionId: state === "active" ? storedSessionId : null,
  };
}

export function useProjectOrchestratorSession(projectName: string) {
  const { projectId, projects } = useTicketProject(projectName);
  const project = useProjectAgentSession(projectId);
  const session = useQuery({
    queryKey: ["session", project.sessionId],
    queryFn: () => getSessionSlim(project.sessionId!),
    enabled: project.sessionId !== null,
    retry: false,
    staleTime: 15_000,
  });
  const { refetch } = project;
  const needsStateRefresh =
    session.data?.archived || (session.error instanceof ApiError && session.error.status === 404);
  useEffect(() => {
    if (needsStateRefresh) void refetch();
  }, [needsStateRefresh, session.dataUpdatedAt, session.errorUpdatedAt, refetch]);
  const canReplace = ["absent", "missing", "archived"].includes(project.state);
  const wrongProject = session.data && session.data.projectId !== projectId;
  const unavailable =
    project.state === "unavailable" || (!canReplace && (wrongProject || session.data?.archived));
  return {
    projectId,
    sessionId: !unavailable && !session.isError && session.data ? project.sessionId : null,
    expectedMainSessionId: project.storedSessionId,
    canReplace: projectId !== null && canReplace && !project.isError,
    isLoading:
      projects.isLoading ||
      (projectId === null && projects.isFetching) ||
      (projectId !== null && project.isPending) ||
      (project.sessionId !== null && session.isPending),
    error:
      projects.error ??
      project.error ??
      (unavailable ? new Error("Orchestrator is unavailable.") : canReplace ? null : session.error),
    refresh: refetch,
  };
}
