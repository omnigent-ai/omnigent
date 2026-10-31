// TanStack Query hooks for thumbs up / thumbs down on assistant responses:
// GET /v1/sessions/{id}/reactions, PUT|DELETE /v1/sessions/{id}/reactions/{responseId}.
// Each user sees and edits only their own reactions; the cache is keyed per
// session and patched optimistically so a click paints before the round trip.
import { QueryClient, QueryClientContext, useMutation, useQuery } from "@tanstack/react-query";
import { useContext } from "react";

import { authenticatedFetch } from "@/lib/identity";

// Used only when no provider is mounted; its queries stay disabled.
const DETACHED_CLIENT = new QueryClient();

export type ReactionRating = "up" | "down";

export interface MessageReaction {
  conversation_id: string;
  response_id: string;
  rating: ReactionRating;
  note: string | null;
  created_by: string | null;
  created_at: number;
  /** Unix microseconds of the last change (change detection, not display). */
  updated_at: number;
}

export function reactionsQueryKey(sessionId: string) {
  return ["reactions", sessionId] as const;
}

export async function fetchReactions(sessionId: string): Promise<MessageReaction[]> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/reactions`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as MessageReaction[];
}

/** The caller's reactions for a session, as a map by response id. Disabled without a session. */
export function useReactions(sessionId: string | null | undefined) {
  // Provider-optional: surfaces rendered outside a QueryClientProvider
  // (isolated tests, embeds) get no reactions instead of a crash. Passing a
  // disabled client keeps the hook order stable.
  const queryClient = useContext(QueryClientContext);
  const query = useQuery(
    {
      queryKey: reactionsQueryKey(sessionId ?? ""),
      queryFn: () => fetchReactions(sessionId!),
      enabled: !!sessionId && !!queryClient,
      staleTime: 10_000,
    },
    queryClient ?? DETACHED_CLIENT,
  );
  const byResponse = new Map<string, MessageReaction>();
  for (const r of query.data ?? []) byResponse.set(r.response_id, r);
  return { ...query, byResponse };
}

function patchCache(
  previous: MessageReaction[] | undefined,
  responseId: string,
  next: MessageReaction | null,
): MessageReaction[] {
  const rest = (previous ?? []).filter((r) => r.response_id !== responseId);
  return next ? [...rest, next] : rest;
}

/** PUT a rating (and optional note); DELETE when `rating` is null. Optimistic. */
export function useSetReaction(sessionId: string | null | undefined) {
  const contextClient = useContext(QueryClientContext);
  const queryClient = contextClient ?? DETACHED_CLIENT;
  const key = reactionsQueryKey(sessionId ?? "");
  return useMutation(
    {
      mutationFn: async (input: {
        responseId: string;
        rating: ReactionRating | null;
        note?: string | null;
      }) => {
        if (!sessionId) throw new Error("Session is not available");
        const url = `/v1/sessions/${encodeURIComponent(sessionId)}/reactions/${encodeURIComponent(input.responseId)}`;
        const res = await authenticatedFetch(
          url,
          input.rating === null
            ? { method: "DELETE" }
            : {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ rating: input.rating, note: input.note ?? null }),
              },
        );
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return input.rating === null ? null : ((await res.json()) as MessageReaction);
      },
      onMutate: async (input) => {
        await queryClient.cancelQueries({ queryKey: key });
        const previous = queryClient.getQueryData<MessageReaction[]>(key);
        const optimistic: MessageReaction | null =
          input.rating === null
            ? null
            : {
                conversation_id: sessionId ?? "",
                response_id: input.responseId,
                rating: input.rating,
                note: input.note ?? null,
                created_by: null,
                created_at: Math.floor(Date.now() / 1000),
                updated_at: Date.now() * 1000,
              };
        queryClient.setQueryData<MessageReaction[]>(key, (old) =>
          patchCache(old, input.responseId, optimistic),
        );
        return { previous };
      },
      onError: (_error, _input, context) => {
        if (context?.previous !== undefined) queryClient.setQueryData(key, context.previous);
      },
      onSuccess: (reaction, input) => {
        queryClient.setQueryData<MessageReaction[]>(key, (old) =>
          patchCache(old, input.responseId, reaction),
        );
      },
    },
    queryClient,
  );
}
