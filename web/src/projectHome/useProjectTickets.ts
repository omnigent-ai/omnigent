import {
  useInfiniteQuery,
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useProjects } from "@/hooks/useConversations";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { createProject } from "@/lib/projectsApi";
import {
  commentOnTicket,
  getTicket,
  getTicketSummary,
  getTicketStages,
  listTicketEvents,
  listTickets,
  listTicketSessions,
  listRelatedTickets,
  linkRelatedTicket,
  setTicketSessionInvolvement,
  DEFAULT_TICKET_STAGES,
  updateTicket,
  type TicketChanges,
  type TicketComment,
  type TicketEvent,
} from "@/lib/projectTicketsApi";

export function useProjectTicketsEnabled() {
  const info = useServerInfo();
  return info !== "loading" && info.project_tickets === true;
}

export function useTicketLifecycleEnabled() {
  const info = useServerInfo();
  return info !== "loading" && info.project_ticket_lifecycle === true;
}

export function useTicketInvolvementEnabled() {
  const info = useServerInfo();
  return info !== "loading" && info.project_ticket_agent_involvement === true;
}

export function useTicketStagesEnabled() {
  const info = useServerInfo();
  return info !== "loading" && info.project_ticket_stages === true;
}

export function useTicketStages(projectId: string | null, includeUsage = false) {
  const supported = useTicketStagesEnabled();
  const defaults = DEFAULT_TICKET_STAGES;
  const query = useQuery({
    queryKey: ["project-tickets", projectId, "stages", includeUsage],
    queryFn: () => getTicketStages(projectId!, includeUsage),
    enabled: projectId !== null && supported,
    refetchInterval: 5000,
  });
  return {
    ...query,
    supported,
    isPending: supported && projectId !== null && query.isPending,
    data: supported && projectId !== null ? query.data : defaults,
  };
}

export function useTicketProject(projectName: string) {
  const projects = useProjects();
  const projectId = projects.data?.find((project) => project.name === projectName)?.id ?? null;
  const client = useQueryClient();
  const ensure = useMutation({
    mutationFn: async () => projectId ?? (await createProject(projectName)).id,
    onSuccess: () => client.invalidateQueries({ queryKey: ["projects"] }),
  });
  return { projectId, projects, ensure };
}

export function useTickets(
  projectId: string | null,
  stageId?: string,
  enabled = true,
  mine = false,
) {
  const supported = useTicketStagesEnabled();
  return useInfiniteQuery({
    queryKey: [
      "project-tickets",
      projectId,
      "board",
      stageId,
      supported,
      ...(mine ? ["mine"] : []),
    ],
    queryFn: ({ pageParam }) =>
      listTickets(projectId!, {
        stage_id: stageId,
        after: pageParam,
        ...(!supported ? { legacy: true } : {}),
        ...(mine ? { mine: true } : {}),
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => (page.has_more ? (page.last_id ?? undefined) : undefined),
    enabled: projectId !== null && enabled,
    refetchInterval: 5000,
  });
}

export function useTicketSummary(projectId: string | null) {
  const info = useServerInfo();
  const supported = info !== "loading" && info.project_ticket_summary === true;
  const query = useQuery({
    queryKey: ["project-tickets", projectId, "summary"],
    queryFn: () => getTicketSummary(projectId!),
    enabled: projectId !== null && supported,
    refetchInterval: 5000,
  });
  return { ...query, data: supported ? query.data : undefined };
}

/** Callers page the explicit selection before requesting it; no board-wide scan. */
export function useTicketSelection(projectId: string | null, ticketIds: readonly string[]) {
  return useQueries({
    queries: ticketIds.map((ticketId) => ({
      queryKey: ["project-tickets", projectId, ticketId],
      queryFn: () => getTicket(projectId!, ticketId),
      enabled: projectId !== null,
      refetchInterval: 5000,
    })),
  });
}

export function useTicket(projectId: string, ticketId: string) {
  return useQuery({
    queryKey: ["project-tickets", projectId, ticketId],
    queryFn: () => getTicket(projectId, ticketId),
    refetchInterval: 5000,
  });
}

export function useTicketEvents(projectId: string, ticketId: string, kind?: TicketEvent["kind"]) {
  return useInfiniteQuery({
    queryKey: ["project-tickets", projectId, ticketId, "events", kind],
    queryFn: ({ pageParam }) => listTicketEvents(projectId, ticketId, pageParam, kind),
    initialPageParam: -1,
    getNextPageParam: (page) => (page.has_more ? page.last_position : undefined),
    refetchInterval: 5000,
  });
}

export function useTicketSessions(projectId: string, ticketId: string) {
  return useInfiniteQuery({
    queryKey: ["project-tickets", projectId, ticketId, "sessions"],
    queryFn: ({ pageParam }) => listTicketSessions(projectId, ticketId, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => (page.has_more ? (page.last_id ?? undefined) : undefined),
    refetchInterval: 5000,
  });
}

export function useRelatedTickets(projectId: string | null, ticketId: string | null) {
  return useInfiniteQuery({
    queryKey: ["project-tickets", projectId, ticketId, "related"],
    queryFn: ({ pageParam }) => listRelatedTickets(projectId!, ticketId!, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => (page.has_more ? (page.last_id ?? undefined) : undefined),
    enabled: projectId !== null && ticketId !== null,
    staleTime: 15_000,
    refetchInterval: 5000,
  });
}

export function useTicketActions(projectId: string) {
  const supported = useTicketStagesEnabled();
  const client = useQueryClient();
  const refresh = () => client.invalidateQueries({ queryKey: ["project-tickets", projectId] });
  const update = useMutation({
    mutationFn: ({ id, changes }: { id: string; changes: TicketChanges }) =>
      supported ? updateTicket(projectId, id, changes) : updateTicket(projectId, id, changes, true),
    onSuccess: refresh,
  });
  const comment = useMutation({
    mutationFn: ({ id, body }: { id: string; body: TicketComment }) =>
      commentOnTicket(projectId, id, body),
    onSuccess: refresh,
  });
  const link = useMutation({
    mutationFn: ({
      id,
      relatedId,
      action,
    }: {
      id: string;
      relatedId: string;
      action: "attach" | "remove";
    }) => linkRelatedTicket(projectId, id, relatedId, action),
    onSuccess: refresh,
  });
  const involvement = useMutation({
    mutationFn: ({ id, sessionId, text }: { id: string; sessionId: string; text: string }) =>
      setTicketSessionInvolvement(projectId, id, sessionId, text),
    onSuccess: refresh,
  });
  return { update, comment, link, involvement };
}
