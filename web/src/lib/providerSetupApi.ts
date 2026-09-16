import { authenticatedFetch } from "@/lib/identity";
import { resolveWebSocketUrl } from "@/lib/host";

export type ProviderFamily = "anthropic" | "openai" | "gemini";
export type ProviderSurface = ProviderFamily | "pi";

export interface SetupProvider {
  name: string;
  kind: string;
  families: string[];
  defaults: string[];
  default_scopes: string[];
  credential_sources: Record<string, string>;
  models: Record<string, string>;
  base_urls: Record<string, string>;
  wire_api?: string | null;
  remove_warning?: string | null;
}

export interface SetupKeyProvider {
  id: string;
  label: string;
  family: string;
  base_url: string;
  wire_api?: string | null;
}

export interface SetupAcpAgent {
  slug: string;
  name: string;
  command: string;
  model?: string | null;
  env_passthrough: string[];
  session_id_mode: string;
  send_model: boolean;
  omnigent_mcp: boolean;
  inject_system_prompt: boolean;
}

export interface SetupHarnessSettings {
  cursor_key_configured: boolean;
  antigravity_key_configured: boolean;
  copilot_key_configured: boolean;
  copilot_host?: string | null;
  opencode_model?: string | null;
}

export interface SetupInventory {
  feature_enabled: boolean;
  /** Server-side release gate. Older hosts may expose feature_enabled instead. */
  mutations_enabled?: boolean;
  providers: SetupProvider[];
  key_providers: SetupKeyProvider[];
  acp_agents: SetupAcpAgent[];
  /** Instruction-only built-in ACP entries from this host's standard setup catalog. */
  builtin_acp?: {
    id: string;
    label: string;
    install_command: string;
    auth_instructions: string;
  }[];
  harness_settings: SetupHarnessSettings;
  dismissed_detections: string[];
  effective_defaults: Record<string, string | null>;
  pi_default_requires_detection?: boolean;
  /** Guided commands whose executable and prerequisites are present on this host. */
  supported_operations: SetupOperationAction[];
}

export interface DetectedConnection {
  name: string;
  kind: string;
  family: string;
  source: string;
  display_name?: string | null;
}

export interface SetupImportPreview {
  source: "openclaw" | "acpx";
  name: string;
  slug: string;
  command: string;
  model?: string | null;
  fingerprint: string;
}

export interface SetupDetectRequest {
  pi_default?: boolean;
  import_path?: string;
  import_source?: "openclaw" | "acpx";
  harness?: SetupStatusHarness;
}

export type SetupStatusHarness =
  | "claude-native"
  | "codex-native"
  | "cursor"
  | "cursor-native"
  | "opencode"
  | "opencode-native"
  | "pi-native"
  | "antigravity"
  | "antigravity-native"
  | "copilot"
  | "qwen"
  | "goose"
  | "hermes"
  | "kiro"
  | "kimi";

export interface SetupHarnessStatus {
  harness: SetupStatusHarness;
  availability: boolean | "binary-missing" | "needs-auth" | "version-too-low";
}

export interface SetupDetection {
  providers: DetectedConnection[];
  pi_default_provider?: string | null;
  pi_default_checked?: boolean;
  harness_status?: SetupHarnessStatus | null;
  imports: SetupImportPreview[];
  models: Record<string, string[]>;
  warnings?: string[];
  default_models?: Record<string, string | null>;
}

interface Credential {
  secret?: string;
  env_var?: string;
}

export type SetupAction =
  | ({ action: "add_key"; provider: string; name?: string; model?: string | null } & Credential)
  | ({
      action: "add_gateway";
      name: string;
      base_url: string;
      families: ("anthropic" | "openai")[];
      wire_api: "chat" | "responses";
      models: Record<string, string>;
    } & Credential)
  | ({ action: "add_bedrock"; name: string; base_url: string; model: string } & Credential)
  | { action: "subscription"; cli: "claude" | "codex" | "pi" }
  | { action: "adopt_detected"; name: string }
  | { action: "set_default"; name: string; surface: ProviderSurface }
  | { action: "remove_provider"; name: string }
  | { action: "dismiss_detection"; name: string; dismissed: boolean }
  | ({ action: "set_harness_key"; harness: "cursor" | "antigravity" | "copilot" } & Credential)
  | { action: "remove_harness_key"; harness: "cursor" | "antigravity" | "copilot" }
  | { action: "set_copilot_host"; host: string | null }
  | { action: "set_opencode_model"; model: string | null }
  | {
      action: "add_acp";
      name: string;
      command: string;
      model?: string;
    }
  | { action: "remove_acp"; slug: string }
  | {
      action: "import_acp";
      source: "openclaw" | "acpx";
      names: string[];
      path?: string;
      fingerprints: Record<string, string>;
    };

export interface SetupActionResult {
  ok: boolean;
  message: string;
  inventory: SetupInventory;
}

export type SetupOperationAction =
  | "claude-login"
  | "codex-login"
  | "cursor-login"
  | "cursor-logout"
  | "antigravity-login"
  | "opencode-login"
  | "qwen-configure"
  | "goose-configure"
  | "hermes-configure"
  | "kiro-login"
  | "kimi-login"
  | "databricks-configure";

export type SetupOperationState =
  "pending" | "running" | "succeeded" | "failed" | "cancelled" | "expired";

export interface SetupOperation {
  operation_id: string;
  state: SetupOperationState;
  action: SetupOperationAction;
  exit_code: number | null;
  error: string | null;
  already_connected?: boolean;
  can_verify?: boolean;
}

export class SetupApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "SetupApiError";
    this.status = status;
  }
}

async function setupFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await authenticatedFetch(path, init);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`.trim();
    try {
      const body = (await res.json()) as { detail?: unknown; error?: { message?: unknown } };
      if (typeof body.detail === "string" && body.detail) detail = body.detail;
      if (typeof body.error?.message === "string" && body.error.message) {
        detail = body.error.message;
      }
    } catch {
      // Preserve the status line for non-JSON failures.
    }
    throw new SetupApiError(detail || "Setup request failed", res.status);
  }
  return (await res.json()) as T;
}

function hostSetupPath(hostId: string): string {
  return `/v1/hosts/${encodeURIComponent(hostId)}/setup`;
}

export function fetchSetupInventory(hostId: string, signal?: AbortSignal): Promise<SetupInventory> {
  return setupFetch(hostSetupPath(hostId), { signal });
}

export function runSetupAction(hostId: string, action: SetupAction): Promise<SetupActionResult> {
  return setupFetch(`${hostSetupPath(hostId)}/actions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(action),
  });
}

export function detectSetup(hostId: string, request?: SetupDetectRequest): Promise<SetupDetection> {
  return setupFetch(`${hostSetupPath(hostId)}/detect`, {
    method: "POST",
    ...(request
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(request),
        }
      : {}),
  });
}

function operationsPath(hostId: string): string {
  return `/v1/hosts/${encodeURIComponent(hostId)}/setup-operations`;
}

export function startSetupOperation(
  hostId: string,
  action: SetupOperationAction,
  parameters: Record<string, unknown> = {},
): Promise<SetupOperation> {
  return setupFetch(operationsPath(hostId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, parameters }),
  });
}

export function fetchSetupOperation(
  hostId: string,
  operationId: string,
  signal?: AbortSignal,
): Promise<SetupOperation> {
  return setupFetch(`${operationsPath(hostId)}/${encodeURIComponent(operationId)}`, { signal });
}

export function cancelSetupOperation(hostId: string, operationId: string): Promise<SetupOperation> {
  return setupFetch(`${operationsPath(hostId)}/${encodeURIComponent(operationId)}`, {
    method: "DELETE",
  });
}

export function verifySetupOperation(hostId: string, operationId: string): Promise<SetupOperation> {
  return setupFetch(`${operationsPath(hostId)}/${encodeURIComponent(operationId)}/verify`, {
    method: "POST",
  });
}

export function setupOperationAttachUrl(hostId: string, operationId: string): string {
  const path =
    `${operationsPath(hostId)}/${encodeURIComponent(operationId)}/attach` +
    `?omnigent_slice_key=${encodeURIComponent(hostId)}`;
  return resolveWebSocketUrl(path);
}
