import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { ApiError, apiErrorFromResponse } from "@/lib/sessionsApi";

export interface CommentReply {
  id: string;
  comment_id: string;
  conversation_id: string;
  author_kind: "user" | "agent";
  body: string;
  created_by: string | null;
  /** Epoch microseconds. */
  created_at: number;
}

interface CommentReplyPage {
  replies: CommentReply[];
  next_cursor: { created_at: number; id: string } | null;
}

function repliesUrl(sessionId: string, commentId: string) {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/comments/${encodeURIComponent(commentId)}/replies`;
}

async function replyResponse(response: Response) {
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export function useCommentReplies(
  sessionId: string,
  commentId: string,
  enabled = true,
  updatedAt?: number,
) {
  return useQuery<CommentReply[]>({
    // A parent fingerprint advance supersedes even an unfinished first read.
    queryKey: ["comments", sessionId, "replies", commentId, updatedAt],
    placeholderData: (previous) => previous,
    enabled,
    queryFn: async ({ signal }): Promise<CommentReply[]> => {
      const replies: CommentReply[] = [];
      let cursor: { created_at: number; id: string } | null = null;
      // The API pages forward from the oldest reply; follow it to find the latest three.
      do {
        const params = new URLSearchParams({ limit: "100" });
        if (cursor) {
          params.set("after_created_at", String(cursor.created_at));
          params.set("after_id", cursor.id);
        }
        // eslint-disable-next-line no-await-in-loop -- Each page needs the preceding cursor.
        const page: CommentReplyPage = await replyResponse(
          // eslint-disable-next-line no-await-in-loop -- Each page needs the preceding cursor.
          await authenticatedFetch(`${repliesUrl(sessionId, commentId)}?${params}`, { signal }),
        );
        replies.push(...page.replies);
        cursor = page.next_cursor;
      } while (cursor);
      return replies;
    },
  });
}

export function useAddCommentReply(sessionId: string, commentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: string): Promise<CommentReply> =>
      replyResponse(
        await authenticatedFetch(repliesUrl(sessionId, commentId), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ body }),
        }),
      ),
    onSuccess: async (reply) => {
      const queryKey = ["comments", sessionId, "replies", commentId];
      await queryClient.cancelQueries({ queryKey });
      queryClient.setQueriesData<CommentReply[]>({ queryKey }, (previous) => [
        ...(previous ?? []).filter((item) => item.id !== reply.id),
        reply,
      ]);
      void queryClient.invalidateQueries({ queryKey: ["comments", sessionId] });
    },
  });
}

export function useDeleteCommentReply(sessionId: string, commentId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (replyId: string): Promise<{ deleted: true }> =>
      replyResponse(
        await authenticatedFetch(
          `${repliesUrl(sessionId, commentId)}/${encodeURIComponent(replyId)}`,
          {
            method: "DELETE",
          },
        ),
      ),
    onSuccess: async (_result, replyId) => {
      const queryKey = ["comments", sessionId, "replies", commentId];
      await queryClient.cancelQueries({ queryKey });
      queryClient.setQueriesData<CommentReply[]>({ queryKey }, (previous) =>
        (previous ?? []).filter((reply) => reply.id !== replyId),
      );
      void queryClient.invalidateQueries({ queryKey: ["comments", sessionId] });
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 404) {
        await queryClient.cancelQueries({
          queryKey: ["comments", sessionId, "replies", commentId],
        });
        void queryClient.invalidateQueries({ queryKey: ["comments", sessionId] });
      }
    },
  });
}
