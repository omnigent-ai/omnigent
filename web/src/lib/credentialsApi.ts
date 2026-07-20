/**
 * Client for the per-user credentials API (Settings → Credentials).
 *
 * Wraps ``GET /v1/credentials``, ``POST /v1/credentials/github/connect``,
 * and ``DELETE /v1/credentials/github`` in a small typed surface shared by
 * the Settings Credentials section.
 *
 * Errors: every helper resolves with a discriminated union instead of
 * throwing, mirroring ``accountsApi.ts``, so the UI renders specific
 * messages without try/catch at every call site.
 */

/** One connected credential, masked — the token never reaches the client. */
export interface CredentialInfo {
  provider: string;
  login: string;
  scopes: string;
  connected_at: number;
}

export interface CredentialsList {
  ok: true;
  credentials: CredentialInfo[];
  /** Whether the deployment has the GitHub OAuth App + encryption key configured. */
  enabled: boolean;
}

export interface CredentialsFailure {
  ok: false;
  error: string;
  status: number;
}

export type CredentialsListResult = CredentialsList | CredentialsFailure;
export type ConnectResult = { ok: true; authorize_url: string } | CredentialsFailure;
export type DisconnectResult = { ok: true } | CredentialsFailure;

const NETWORK_FAILURE: CredentialsFailure = {
  ok: false,
  error: "Could not reach the server. Check your connection.",
  status: 0,
};

async function failureFrom(res: Response, fallback: string): Promise<CredentialsFailure> {
  let message = fallback;
  if (res.status >= 500) {
    message = "Server error. Try again in a moment.";
  } else {
    try {
      const data = (await res.json()) as { error?: string };
      if (data.error) message = data.error;
    } catch {
      // keep fallback
    }
  }
  return { ok: false, error: message, status: res.status };
}

/** GET /v1/credentials — the caller's connected credentials. */
export async function listCredentials(): Promise<CredentialsListResult> {
  let res: Response;
  try {
    res = await fetch("/v1/credentials");
  } catch {
    return NETWORK_FAILURE;
  }
  if (res.ok) {
    const data = (await res.json()) as Omit<CredentialsList, "ok">;
    return { ok: true, ...data };
  }
  return failureFrom(res, "Could not load credentials.");
}

/** POST /v1/credentials/github/connect — start the GitHub OAuth flow. */
export async function connectGithub(): Promise<ConnectResult> {
  let res: Response;
  try {
    res = await fetch("/v1/credentials/github/connect", { method: "POST" });
  } catch {
    return NETWORK_FAILURE;
  }
  if (res.ok) {
    const data = (await res.json()) as { authorize_url: string };
    return { ok: true, authorize_url: data.authorize_url };
  }
  return failureFrom(res, "Could not start the GitHub connection.");
}

/** DELETE /v1/credentials/github — disconnect GitHub. */
export async function disconnectGithub(): Promise<DisconnectResult> {
  let res: Response;
  try {
    res = await fetch("/v1/credentials/github", { method: "DELETE" });
  } catch {
    return NETWORK_FAILURE;
  }
  if (res.ok) return { ok: true };
  return failureFrom(res, "Could not disconnect GitHub.");
}
