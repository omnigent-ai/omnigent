/**
 * Runtime capabilities probe.
 *
 * Hits ``GET /v1/info`` once at app boot to learn what the server
 * supports — currently just whether the accounts auth provider is
 * active. The SPA uses the result to decide whether to register
 * ``/login`` / ``/register`` / ``/members`` routes and whether to
 * render the ``AccountMenu``.
 *
 * This is the single gate for accounts UI in the SPA. When the
 * internal hosted product (header / OIDC) syncs from this repo
 * and serves this bundle, ``/v1/info`` returns
 * ``accounts_enabled: false`` and none of the accounts routes
 * are reachable — the bundle behaves identically to a pre-PR-2008
 * build for those deploys.
 *
 * Mirrors the ``identity.ts`` resolve-once-then-cache pattern.
 * Unauthed by design — must work before any cookie is present.
 */

import { hostFetch } from "./host";

/** Shape of the response from ``GET /v1/info``. */
export interface ServerInfo {
  accounts_enabled: boolean;
  login_url: string | null;
  /**
   * True when accounts mode is on but no admin has been claimed yet —
   * the SPA shows the first-run "Create admin" form instead of login.
   * Flips to false the moment /auth/setup (or any login) creates the
   * first admin.
   */
  needs_setup: boolean;
  /**
   * True on Databricks/internal deployments (the server's internal lakebox
   * CLI is present). Gates Databricks-only UI hints — the "Databricks Lakebox"
   * connect tab in the CLI command snippets. False
   * on the OSS build, where those modules are excluded from the export, so the
   * SPA shows the clean, provider-agnostic hints.
   */
  databricks_features: boolean;
  /**
   * True when the server can provision cloud-sandbox hosts for
   * ``host_type: "managed"`` session creates (a ``sandbox:`` config with a
   * launch-capable provider is wired). Gates the sandbox option in
   * the new-session host picker.
   */
  managed_sandboxes_enabled: boolean;
  /**
   * Short name of the backing sandbox provider (e.g. ``"modal"``,
   * ``"lakebox"``) used to label the new-session sandbox option per
   * provider ("Modal Sandbox" / "Databricks Sandbox"). ``null`` when
   * the server names no provider (e.g. an embedding deployment that
   * left it unset), in which case the UI shows the generic
   * "New Sandbox" label. Only meaningful when
   * ``managed_sandboxes_enabled`` is true.
   */
  sandbox_provider: string | null;
  /**
   * Installed omnigent server version (same value as ``/api/version``),
   * e.g. ``"0.3.0.dev0"``. Shown in the session info popover's version
   * footer. ``null`` only when the probe failed (the OFF sentinel) — a
   * live server always reports it.
   */
  server_version: string | null;
  /**
   * True when the server has a routing client configured
   * (``OMNIGENT_SMART_ROUTING=1`` + ``llm:`` config). Hidden by default.
   */
  smart_routing_enabled: boolean;
}

/** Sentinel used when the probe fails — accounts is off, no login URL. */
const _OFF: ServerInfo = {
  accounts_enabled: false,
  login_url: null,
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
  server_version: null,
  smart_routing_enabled: false,
};

let _cached: ServerInfo | null = null;
let _pending: Promise<ServerInfo> | null = null;

/**
 * Fetch ``/v1/info`` once and cache the result.
 *
 * Resolves to ``_OFF`` on any failure (network error, non-JSON,
 * 5xx). The frontend treats "no probe result" as "accounts is
 * off" — failing closed prevents the accounts UI from rendering
 * against a server that doesn't actually support it.
 */
export async function resolveServerInfo(): Promise<ServerInfo> {
  if (_cached !== null) return _cached;
  if (_pending !== null) return _pending;
  _pending = (async () => {
    try {
      // Route through the host transport (`hostFetch`) so the embed hits the
      // proxied omnigent API; standalone `hostFetch` falls back to plain
      // `fetch("/v1/info")`, preserving the original behavior.
      const res = await hostFetch("/v1/info");
      if (res.ok) {
        const data = (await res.json()) as Partial<ServerInfo>;
        _cached = {
          accounts_enabled: data.accounts_enabled === true,
          login_url: typeof data.login_url === "string" ? data.login_url : null,
          needs_setup: data.needs_setup === true,
          databricks_features: data.databricks_features === true,
          managed_sandboxes_enabled: data.managed_sandboxes_enabled === true,
          sandbox_provider:
            typeof data.sandbox_provider === "string" ? data.sandbox_provider : null,
          server_version: typeof data.server_version === "string" ? data.server_version : null,
          smart_routing_enabled: data.smart_routing_enabled === true,
        };
        return _cached;
      }
    } catch {
      // Network failure — fall through to the off sentinel.
    }
    _cached = _OFF;
    return _cached;
  })();
  return _pending;
}

/**
 * Synchronous read of the cached probe.
 *
 * Returns ``null`` if :func:`resolveServerInfo` hasn't been
 * awaited yet. Components that need the value at render time
 * should consume the React context populated from the awaited
 * result (see ``CapabilitiesProvider`` in ``main.tsx``) rather
 * than calling this directly.
 */
export function getCachedServerInfo(): ServerInfo | null {
  return _cached;
}

/**
 * Known provider id → display name for the sandbox label. Providers
 * not listed here fall back to a title-cased id so a newly-wired
 * provider still reads sensibly without a frontend change.
 */
const _SANDBOX_PROVIDER_NAMES: Record<string, string> = {
  modal: "Modal",
  lakebox: "Databricks",
  daytona: "Daytona",
  e2b: "E2B",
};

/**
 * Label for the new-session sandbox option, named per provider.
 *
 * Returns e.g. "Modal Sandbox" or "Databricks Sandbox" when the
 * server reports a provider, and the generic "New Sandbox" when it
 * names none (``null``) — the same wording the UI used before
 * providers were surfaced.
 */
export function sandboxOptionLabel(provider: string | null): string {
  if (!provider) return "New Sandbox";
  const name =
    _SANDBOX_PROVIDER_NAMES[provider] ?? provider.charAt(0).toUpperCase() + provider.slice(1);
  return `${name} Sandbox`;
}
