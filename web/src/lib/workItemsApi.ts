/**
 * Typed client for the `/v1/work-items` endpoints.
 * Mirrors `omnigent/server/routes/work_items.py`.
 */

import { authenticatedFetch } from "./identity";

/** Lifecycle states, matching `omnigent.entities.WORK_ITEM_STATUSES`. */
export const WORK_ITEM_STATUSES = [
  "new",
  "planned",
  "in_progress",
  "blocked",
  "needs_review",
  "done",
] as const;

export type WorkItemStatus = (typeof WORK_ITEM_STATUSES)[number];

/** A work item returned by the REST API. */
export interface WorkItem {
  id: string;
  object: "work_item";
  source: string;
  external_id: string | null;
  dedup_key: string;
  title: string;
  body: string | null;
  status: string;
  pr_url: string | null;
  conversation_id: string | null;
  assignee_user_id: string | null;
  created_by: string | null;
  plan: string | null;
  created_at: number;
  updated_at: number | null;
}

export interface ListWorkItemsParams {
  status?: string;
  conversationId?: string;
}

export async function listWorkItems(params: ListWorkItemsParams = {}): Promise<WorkItem[]> {
  const qs = new URLSearchParams();
  if (params.status) qs.set("status", params.status);
  if (params.conversationId) qs.set("conversation_id", params.conversationId);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await authenticatedFetch(`/v1/work-items${suffix}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { object: string; data: WorkItem[] };
  return body.data;
}

export interface UpdateWorkItemPatch {
  title?: string;
  body?: string;
  status?: string;
  pr_url?: string;
  conversation_id?: string;
  assignee_user_id?: string;
  plan?: string;
}

export async function updateWorkItem(id: string, patch: UpdateWorkItemPatch): Promise<WorkItem> {
  const res = await authenticatedFetch(`/v1/work-items/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as WorkItem;
}

export async function deleteWorkItem(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/work-items/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
}
