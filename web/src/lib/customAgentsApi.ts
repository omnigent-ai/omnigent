import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "./identity";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";

export const CUSTOM_AGENTS_QUERY_KEY = ["custom-agents"] as const;
export const AGENT_TEMPLATE_LABEL = "omnigent:agent-template-id";

export interface CustomAgent {
  id: string;
  name: string;
  description: string | null;
  harness: string | null;
  model: string | null;
  version: number;
  created_at: number;
  updated_at: number | null;
}

export interface CustomAgentDetail extends CustomAgent {
  instructions: string | null;
}

async function checked(response: Response): Promise<Response> {
  if (response.ok) return response;
  let message = `Agent request failed (${response.status})`;
  try {
    const body = await response.json();
    if (typeof body.detail === "string") message = body.detail;
  } catch {
    /* Keep the status when the server returns a non-JSON error. */
  }
  throw new Error(message);
}

export async function listCustomAgents(): Promise<CustomAgent[]> {
  const result: CustomAgent[] = [];
  let hasMore = true;
  /* oxlint-disable no-await-in-loop -- Each page starts at the preceding page's offset. */
  while (hasMore) {
    const response = await checked(
      await authenticatedFetch(`/v1/custom-agents?limit=100&offset=${result.length}`),
    );
    const page = (await response.json()) as { data: CustomAgent[]; has_more: boolean };
    if (!Array.isArray(page.data) || typeof page.has_more !== "boolean") {
      throw new Error("Invalid custom Agent catalog");
    }
    result.push(...page.data);
    hasMore = page.has_more && page.data.length > 0;
  }
  /* oxlint-enable no-await-in-loop */
  return result;
}

export function useCustomAgents(enabled = true) {
  return useQuery({
    queryKey: CUSTOM_AGENTS_QUERY_KEY,
    queryFn: listCustomAgents,
    enabled,
    staleTime: 30_000,
    retry: false,
  });
}

export function customAgentForPicker(agent: CustomAgent): AvailableAgent {
  return {
    id: agent.id,
    name: agent.name,
    display_name: agent.name,
    description: agent.description,
    harness: agent.harness,
    skills: [],
    builtin: false,
    created_at: agent.created_at,
  };
}

export async function getCustomAgent(id: string): Promise<CustomAgentDetail> {
  return (
    await checked(await authenticatedFetch(`/v1/custom-agents/${encodeURIComponent(id)}`))
  ).json();
}

export async function createCustomAgent(bundle: File): Promise<CustomAgentDetail> {
  const body = new FormData();
  body.append("bundle", bundle);
  return (
    await checked(await authenticatedFetch("/v1/custom-agents", { method: "POST", body }))
  ).json();
}

export async function importCustomAgent(sessionId: string): Promise<CustomAgentDetail> {
  return (
    await checked(
      await authenticatedFetch("/v1/custom-agents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_session_id: sessionId }),
      }),
    )
  ).json();
}

export async function updateCustomAgent(
  id: string,
  changes: Pick<CustomAgentDetail, "name" | "description" | "instructions" | "version">,
): Promise<CustomAgentDetail> {
  return (
    await checked(
      await authenticatedFetch(`/v1/custom-agents/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      }),
    )
  ).json();
}

export async function deleteCustomAgent(id: string): Promise<void> {
  await checked(
    await authenticatedFetch(`/v1/custom-agents/${encodeURIComponent(id)}`, { method: "DELETE" }),
  );
}

export async function customAgentBundle(id: string): Promise<File> {
  const response = await checked(
    await authenticatedFetch(`/v1/custom-agents/${encodeURIComponent(id)}/contents`, {
      cache: "no-store",
    }),
  );
  const type = response.headers.get("Content-Type") ?? "application/gzip";
  return new File(
    [await response.blob()],
    type === "application/x-tar" ? "agent.tar" : "agent.tar.gz",
    { type },
  );
}
