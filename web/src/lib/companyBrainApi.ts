import { authenticatedFetch } from "./identity";

export type CompanyBrainProvider = "google" | "slack" | "notion";
export type ConnectionStatus = "connected" | "needs_reconnect" | "disconnected" | "error";
export type SelectionState = "active" | "paused" | "disconnected";
export type SyncRunStatus = "pending" | "running" | "succeeded" | "failed" | "skipped";

export interface BrainInstallation {
  id: string;
  status: "provisioning" | "ready" | "degraded" | "disabled";
  repoUrl: string | null;
  mcpUrl: string | null;
  createdAt: number;
  updatedAt: number | null;
}

export interface IntegrationConnection {
  id: string;
  provider: CompanyBrainProvider;
  accountLabel: string | null;
  grantedScopes: string[];
  status: ConnectionStatus;
  lastError: string | null;
  createdAt: number;
  updatedAt: number | null;
}

export interface IntegrationSelection {
  id: string;
  connectionId: string;
  externalResourceId: string;
  resourceName: string;
  resourceType: string;
  sourceUrl: string | null;
  transformProfile: string;
  visibilityClass: "org-shared";
  rrule: string | null;
  timezone: string;
  state: SelectionState;
  lastSyncedAt: number | null;
  pageCount: number;
  lastError: string | null;
  createdAt: number;
  updatedAt: number | null;
}

export interface IntegrationSyncRun {
  id: string;
  connectionId: string;
  selectionId: string;
  status: SyncRunStatus;
  trigger: "manual" | "schedule" | "retry";
  fetchedCount: number;
  changedCount: number;
  deletedCount: number;
  skippedCount: number;
  commitSha: string | null;
  error: string | null;
  scheduledAt: number | null;
  startedAt: number | null;
  finishedAt: number | null;
  createdAt: number;
}

export interface CompanyBrainState {
  installation: BrainInstallation | null;
  connections: IntegrationConnection[];
  selections: IntegrationSelection[];
  runs: IntegrationSyncRun[];
}

export interface ProviderStatus {
  id: CompanyBrainProvider;
  configured: boolean;
  scopes: string[];
}

export interface ProviderResource {
  id: string;
  name: string;
  resourceType: string;
  sourceUrl: string | null;
  orgShared: true;
  metadata: Record<string, string>;
}

export interface BrainPreviewDocument {
  provider: CompanyBrainProvider;
  title: string;
  markdown: string;
  canonicalSourceUrl: string;
  contentSha256: string;
  transformSchemaVersion: string;
  visibilityClass: "org-shared";
}

interface InstallationWire {
  id: string;
  status: BrainInstallation["status"];
  repo_url: string | null;
  mcp_url: string | null;
  created_at: number;
  updated_at: number | null;
}

interface ConnectionWire {
  id: string;
  provider: CompanyBrainProvider;
  account_label: string | null;
  granted_scopes: string[];
  status: ConnectionStatus;
  last_error: string | null;
  created_at: number;
  updated_at: number | null;
}

interface SelectionWire {
  id: string;
  connection_id: string;
  external_resource_id: string;
  resource_name: string;
  resource_type: string;
  source_url: string | null;
  transform_profile: string;
  visibility_class: "org-shared";
  rrule: string | null;
  timezone: string;
  state: SelectionState;
  last_synced_at: number | null;
  page_count: number;
  last_error: string | null;
  created_at: number;
  updated_at: number | null;
}

interface RunWire {
  id: string;
  connection_id: string;
  selection_id: string;
  status: SyncRunStatus;
  trigger: IntegrationSyncRun["trigger"];
  fetched_count: number;
  changed_count: number;
  deleted_count: number;
  skipped_count: number;
  commit_sha: string | null;
  error: string | null;
  scheduled_at: number | null;
  started_at: number | null;
  finished_at: number | null;
  created_at: number;
}

export class CompanyBrainApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = "CompanyBrainApiError";
    this.status = status;
    this.code = code;
  }
}

async function readJson<T>(response: Response): Promise<T> {
  if (response.ok) return (await response.json()) as T;
  let message = `${response.status} ${response.statusText}`;
  let code: string | null = null;
  try {
    const body = (await response.json()) as { error?: { code?: string; message?: string } };
    message = body.error?.message ?? message;
    code = body.error?.code ?? null;
  } catch {
    // Keep the status-line fallback for empty or non-JSON responses.
  }
  throw new CompanyBrainApiError(message, response.status, code);
}

function connectionFromWire(value: ConnectionWire): IntegrationConnection {
  return {
    id: value.id,
    provider: value.provider,
    accountLabel: value.account_label,
    grantedScopes: value.granted_scopes,
    status: value.status,
    lastError: value.last_error,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
  };
}

function selectionFromWire(value: SelectionWire): IntegrationSelection {
  return {
    id: value.id,
    connectionId: value.connection_id,
    externalResourceId: value.external_resource_id,
    resourceName: value.resource_name,
    resourceType: value.resource_type,
    sourceUrl: value.source_url,
    transformProfile: value.transform_profile,
    visibilityClass: value.visibility_class,
    rrule: value.rrule,
    timezone: value.timezone,
    state: value.state,
    lastSyncedAt: value.last_synced_at,
    pageCount: value.page_count,
    lastError: value.last_error,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
  };
}

function runFromWire(value: RunWire): IntegrationSyncRun {
  return {
    id: value.id,
    connectionId: value.connection_id,
    selectionId: value.selection_id,
    status: value.status,
    trigger: value.trigger,
    fetchedCount: value.fetched_count,
    changedCount: value.changed_count,
    deletedCount: value.deleted_count,
    skippedCount: value.skipped_count,
    commitSha: value.commit_sha,
    error: value.error,
    scheduledAt: value.scheduled_at,
    startedAt: value.started_at,
    finishedAt: value.finished_at,
    createdAt: value.created_at,
  };
}

function resourceToWire(resource: ProviderResource) {
  return {
    id: resource.id,
    name: resource.name,
    resource_type: resource.resourceType,
    source_url: resource.sourceUrl,
    org_shared: true,
    metadata: resource.metadata,
  };
}

export async function getCompanyBrain(): Promise<CompanyBrainState> {
  const body = await readJson<{
    installation: InstallationWire | null;
    connections: ConnectionWire[];
    selections: SelectionWire[];
    runs: RunWire[];
  }>(await authenticatedFetch("/v1/company-brain"));
  return {
    installation: body.installation
      ? {
          id: body.installation.id,
          status: body.installation.status,
          repoUrl: body.installation.repo_url,
          mcpUrl: body.installation.mcp_url,
          createdAt: body.installation.created_at,
          updatedAt: body.installation.updated_at,
        }
      : null,
    connections: body.connections.map(connectionFromWire),
    selections: body.selections.map(selectionFromWire),
    runs: body.runs.map(runFromWire),
  };
}

export async function listCompanyBrainProviders(): Promise<ProviderStatus[]> {
  const body = await readJson<{ providers: ProviderStatus[] }>(
    await authenticatedFetch("/v1/company-brain/providers"),
  );
  return body.providers;
}

export async function startCompanyBrainOAuth(
  provider: CompanyBrainProvider,
): Promise<{ authorizeUrl: string }> {
  const redirectUri = `${window.location.origin}/v1/company-brain/oauth/${provider}/callback`;
  const body = await readJson<{ authorize_url: string }>(
    await authenticatedFetch(`/v1/company-brain/oauth/${provider}/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ redirect_uri: redirectUri, return_to: "/settings/company-brain" }),
    }),
  );
  return { authorizeUrl: body.authorize_url };
}

export async function listCompanyBrainResources(connectionId: string): Promise<ProviderResource[]> {
  const body = await readJson<{
    resources: {
      id: string;
      name: string;
      resource_type: string;
      source_url: string | null;
      org_shared: true;
      metadata: Record<string, string>;
    }[];
  }>(
    await authenticatedFetch(
      `/v1/company-brain/connections/${encodeURIComponent(connectionId)}/resources`,
    ),
  );
  return body.resources.map((item) => ({
    id: item.id,
    name: item.name,
    resourceType: item.resource_type,
    sourceUrl: item.source_url,
    orgShared: item.org_shared,
    metadata: item.metadata,
  }));
}

export async function previewCompanyBrainResources(
  connectionId: string,
  resources: ProviderResource[],
): Promise<BrainPreviewDocument[]> {
  const body = await readJson<{
    documents: {
      provider: CompanyBrainProvider;
      title: string;
      markdown: string;
      canonical_source_url: string;
      content_sha256: string;
      transform_schema_version: string;
      visibility_class: "org-shared";
    }[];
  }>(
    await authenticatedFetch(
      `/v1/company-brain/connections/${encodeURIComponent(connectionId)}/preview`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resources: resources.map(resourceToWire) }),
      },
    ),
  );
  return body.documents.map((item) => ({
    provider: item.provider,
    title: item.title,
    markdown: item.markdown,
    canonicalSourceUrl: item.canonical_source_url,
    contentSha256: item.content_sha256,
    transformSchemaVersion: item.transform_schema_version,
    visibilityClass: item.visibility_class,
  }));
}

export async function activateCompanyBrainResources(
  connectionId: string,
  resources: ProviderResource[],
  rrule: string | null,
): Promise<IntegrationSelection[]> {
  const body = await readJson<{ selections: SelectionWire[] }>(
    await authenticatedFetch(
      `/v1/company-brain/connections/${encodeURIComponent(connectionId)}/activate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resources: resources.map(resourceToWire), rrule, timezone: "UTC" }),
      },
    ),
  );
  return body.selections.map(selectionFromWire);
}

export async function updateCompanyBrainSelection(
  id: string,
  input: { state?: "active" | "paused"; rrule?: string | null; timezone?: string },
): Promise<IntegrationSelection> {
  return selectionFromWire(
    await readJson<SelectionWire>(
      await authenticatedFetch(`/v1/company-brain/selections/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      }),
    ),
  );
}

export async function syncCompanyBrainSelection(
  id: string,
  retry = false,
): Promise<IntegrationSyncRun> {
  const action = retry ? "retry" : "sync";
  return runFromWire(
    await readJson<RunWire>(
      await authenticatedFetch(`/v1/company-brain/selections/${encodeURIComponent(id)}/${action}`, {
        method: "POST",
      }),
    ),
  );
}

export async function disconnectCompanyBrainConnection(id: string): Promise<IntegrationConnection> {
  return connectionFromWire(
    await readJson<ConnectionWire>(
      await authenticatedFetch(`/v1/company-brain/connections/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }),
    ),
  );
}
