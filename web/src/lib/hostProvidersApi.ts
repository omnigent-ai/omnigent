// Typed client for the host provider / agent-spec pin endpoints (config
// control plane, part of issue #7134). Mirrors the `/v1/hosts/{id}/providers*`
// and `/v1/hosts/{id}/agent-specs*` routes in
// `omnigent/server/routes/hosts.py`; the wire payloads are defined by
// `omnigent/host/provider_ops.py`.
//
// Secret handling mirrors the server: an inline `api_key` value is only ever
// SENT (when the user types one) and never returned — listings carry
// `api_key_set: true` instead. `api_key_ref` / `auth_command` are references,
// not secrets, and pass through verbatim.

import { authenticatedFetch } from "./identity";

/** Provider kinds accepted by `providers:` entries. */
export type ProviderKind =
  "key" | "gateway" | "local" | "subscription" | "databricks" | "cli-config" | "bedrock";

/** The inline credential families a provider entry may declare. */
export type ProviderFamily = "anthropic" | "openai" | "gemini";

/**
 * One redacted provider entry as the wire returns it: family blocks carry
 * `api_key_set: true` instead of a secret value; `api_key_ref` and
 * `auth_command` are references the host resolves server/host-side.
 */
export interface HostProvider {
  name: string;
  kind?: ProviderKind;
  default?: boolean | string | string[];
  anthropic?: Record<string, unknown>;
  openai?: Record<string, unknown>;
  gemini?: Record<string, unknown>;
  [key: string]: unknown;
}

/** One host-local agent spec (`~/.omnigent/agents/`) and its current pins. */
export interface HostAgentSpec {
  name: string;
  harness: string | null;
  model: string | null;
  auth: { type: string; name?: string } | null;
  spec_version: number | string | null;
  path: string;
}

export interface ProviderTestResult {
  name: string;
  family: string;
  endpoint: string;
  http_status?: number;
  ok: boolean;
  latency_ms?: number;
  models?: string[];
  error?: string;
}

/** Typed HTTP error carrying the server's `detail` (FastAPI shape). */
export class HostProvidersApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "HostProvidersApiError";
    this.status = status;
  }
}

async function errorFromResponse(res: Response): Promise<HostProvidersApiError> {
  let message = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) message = body.detail;
  } catch {
    // non-JSON body — keep the status line
  }
  return new HostProvidersApiError(message, res.status);
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as T;
}

/** List the host's providers, secrets redacted. */
export async function fetchHostProviders(hostId: string): Promise<HostProvider[]> {
  const res = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/providers`);
  const body = await jsonOrThrow<{ providers: HostProvider[] }>(res);
  return body.providers;
}

/** Create or replace one provider entry (validated host-side). */
export async function upsertHostProvider(
  hostId: string,
  name: string,
  entry: Record<string, unknown>,
): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/providers/${encodeURIComponent(name)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entry }),
    },
  );
  await jsonOrThrow<unknown>(res);
}

/** Delete one provider entry. */
export async function deleteHostProvider(hostId: string, name: string): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/providers/${encodeURIComponent(name)}`,
    {
      method: "DELETE",
    },
  );
  await jsonOrThrow<unknown>(res);
}

/** Probe the provider's endpoint with its own credential. */
export async function testHostProvider(hostId: string, name: string): Promise<ProviderTestResult> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/providers/${encodeURIComponent(name)}/test`,
    { method: "POST" },
  );
  return jsonOrThrow<ProviderTestResult>(res);
}

/** List the host-local agent specs with their current pins. */
export async function fetchHostAgentSpecs(hostId: string): Promise<HostAgentSpec[]> {
  const res = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/agent-specs`);
  const body = await jsonOrThrow<{ agents: HostAgentSpec[] }>(res);
  return body.agents;
}

export interface AgentPinInput {
  provider?: string | null;
  model?: string | null;
}

/** Pin an agent spec's provider and/or model. */
export async function pinHostAgent(
  hostId: string,
  agentName: string,
  pin: AgentPinInput,
): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/agent-specs/${encodeURIComponent(agentName)}/pin`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: pin.provider ?? null, model: pin.model ?? null }),
    },
  );
  await jsonOrThrow<unknown>(res);
}

/** Clear an agent spec's provider and model pins. */
export async function clearHostAgentPin(hostId: string, agentName: string): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/agent-specs/${encodeURIComponent(agentName)}/pin`,
    { method: "DELETE" },
  );
  await jsonOrThrow<unknown>(res);
}
