import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export interface ForumParticipantInput {
  agent_id: string;
  display_name?: string;
  harness: string;
  model: string;
}
export interface ForumParticipant {
  session_id: string;
  harness: string;
  model: string;
  name: string;
  error: string | null;
  retry_at: number;
  paused?: boolean;
}
export interface Forum {
  id: string;
  title: string | null;
  permission_level: number | null;
  workspace: string | null;
  status: "blank" | "running" | "paused" | "stopped" | "limit" | "deleting" | "suspended";
  participants: ForumParticipant[];
  max_responses: number;
  completed: number;
  stop_reason?: "human" | "consensus" | "limit" | null;
  completion_votes?: string[];
  active_index: number | null;
  posts: {
    id: string;
    author: string;
    display_name?: string;
    role: string;
    text: string;
    completion_vote?: "DONE" | "CONTINUE";
    created_at: number;
    session_id?: string;
  }[];
}
export async function forumRequest<T>(path: string, body?: unknown): Promise<T> {
  const response = await authenticatedFetch(
    `/v1/forums${path}`,
    body === undefined
      ? undefined
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json() as Promise<T>;
}
export async function listForums(): Promise<Forum[]> {
  const result: Forum[] = [];
  let cursor: string | null = null;
  do {
    // Each page supplies the cursor needed to request the next page.
    // eslint-disable-next-line no-await-in-loop
    const page: { data: Forum[]; next_cursor: string | null } = await forumRequest(
      cursor ? `?after=${encodeURIComponent(cursor)}` : "",
    );
    result.push(...page.data);
    cursor = page.next_cursor;
  } while (cursor);
  return result;
}
