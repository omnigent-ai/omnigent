/**
 * Typed client for the `/v1/canvas/{conversation_id}` endpoint.
 * Mirrors `omnigent/server/routes/canvas.py`.
 */

import { authenticatedFetch } from "./identity";

/** A conversation's canvas artifact (set by the agent's set_canvas tool). */
export interface Canvas {
  id: string;
  object: "canvas";
  conversation_id: string;
  title: string;
  content: string;
  content_type: "html" | "markdown";
  created_at: number;
  updated_at: number | null;
}

/**
 * Fetch a conversation's canvas, or ``null`` when none is set (the endpoint
 * 404s — modeled as "no canvas" rather than an error so the UI can simply
 * hide the tab).
 */
export async function getCanvas(conversationId: string): Promise<Canvas | null> {
  const res = await authenticatedFetch(`/v1/canvas/${encodeURIComponent(conversationId)}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Canvas;
}
