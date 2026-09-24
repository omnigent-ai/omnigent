import { authenticatedFetch } from "./identity";

export interface RegistryService {
  id: string;
  title: string;
  description: string;
  auth: "none" | "bearer" | "oauth" | "github" | "databricks";
  connected: boolean;
  tools: string[];
  connect_provider: string;
}

export async function registryRequest<T>(path = "", init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(`/v1/mcp-registry/services${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : "MCP registry request failed");
  }
  return response.json() as Promise<T>;
}
