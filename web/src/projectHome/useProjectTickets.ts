import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useProjects } from "@/hooks/useConversations";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { createProject } from "@/lib/projectsApi";
import {
  commentOnTicket,
  getTicket,
  listTicketEvents,
  listTickets,
  listTicketSessions,
  listRelatedTickets,
  linkRelatedTicket,
  setTicketSessionInvolvement,
  TICKET_STATUSES,
  updateTicket,
  type TicketChanges,
  type TicketComment,
  type TicketEvent,
  type TicketStatus,
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

export function useTicketStatuses() {
  const lifecycle = useTicketLifecycleEnabled();
  return TICKET_STATUSES.filter((status) => lifecycle || !["planning", "review"].includes(status));
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

export function useTickets(projectId: string | null, status?: TicketStatus) {
  return useInfiniteQuery({
    queryKey: ["project-tickets", projectId, "board", status],
    queryFn: ({ pageParam }) => listTickets(projectId!, { status, after: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => (page.has_more ? (page.last_id ?? undefined) : undefined),
    enabled: projectId !== null,
    refetchInterval: 5000,
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
  const client = useQueryClient();
  const refresh = () => client.invalidateQueries({ queryKey: ["project-tickets", projectId] });
  const update = useMutation({
    mutationFn: ({ id, changes }: { id: string; changes: TicketChanges }) =>
      updateTicket(projectId, id, changes),
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
