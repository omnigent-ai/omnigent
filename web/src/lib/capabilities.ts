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

/**
 * Server session-sharing policy (mirrors the backend ``SharingMode``):
 * ``"on"`` allows grants at any level, ``"read_only"`` caps grants at
 * view, ``"restricted_read_only"`` also caps at view but the server
 * additionally blocks sharing sessions whose cwd is a home/root
 * directory (enforced server-side), and ``"off"`` disables all new
 * grants (the SPA hides the Share control). Fails open to ``"on"`` for
 * an unknown/missing value.
 */
export type SharingMode = "on" | "read_only" | "restricted_read_only" | "off";
const SHARING_MODES: readonly SharingMode[] = ["on", "read_only", "restricted_read_only", "off"];

/**
 * Which router can back a Smart Routing pick on this server.
 *
 * ``external`` is the Databricks AI-Gateway ``task_v1`` router (only usable for
 * a harness family the host runs through the gateway); ``oss`` is the built-in
 * judge, which needs no gateway backing. A server that predates the field
 * reports neither, so the parser degrades from ``smart_routing_enabled``.
 */
export interface SmartRoutingSources {
  external: boolean;
  oss: boolean;
}

export interface Branding {
  app_name: string | null;
  heading: string | null;
  logos: {
    main: string | null;
    loading: string | null;
    favicon: string | null;
  };
  powered_by: boolean;
}

/** Release features understood by this frontend build. */
export type FeatureKey = "usage_page" | "harness_install";

/** Deployment-wide release-feature values advertised by the server. */
export type FeatureValues = Record<string, boolean>;

/** Shape of the response from ``GET /v1/info``. */
export interface ServerInfo {
  accounts_enabled: boolean;
  /**
   * True only on an explicit single-user local runtime
   * (``OMNIGENT_LOCAL_SINGLE_USER=1``). This is the sole signal that
   * separates a genuine one-user server from a multi-user header-auth
   * deploy (e.g. an SSO proxy injecting ``X-Forwarded-Email``) — both
   * report ``accounts_enabled: false`` / ``login_url: null``. Gates
   * account/sharing chrome that's meaningless without other users.
   * Fails to ``false`` (multi-user) so a failed probe never hides it.
   */
  single_user: boolean;
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
   * Every launch-capable sandbox provider, in configured order — one
   * new-session picker row each. Empty or absent falls back to the single
   * ``sandbox_provider`` row. Read via :func:`sandboxProviderOptions`.
   */
  sandbox_providers?: string[];
  /**
   * Server session-sharing policy. Drives whether the SPA shows the
   * Share control (``"on"``), restricts it to read-only invites
   * (``"read_only"``), or hides it entirely (``"off"``), in lockstep
   * with the server-side grant gate. Fails open to ``"on"``.
   */
  sharing_mode: SharingMode;
  /**
   * Whether public (anyone-with-the-link) read access may be granted.
   * Independent of ``sharing_mode`` — drives whether the Share modal shows
   * the "Public access" toggle. Fails open to ``true``.
   */
  public_sharing_enabled: boolean;
  /**
   * Installed omnigent server version (same value as ``/api/version``),
   * e.g. ``"0.3.0.dev0"``. Shown in the session info popover's version
   * footer. ``null`` only when the probe failed (the OFF sentinel) — a
   * live server always reports it.
   */
  server_version: string | null;
  /**
   * True when the server has a routing client configured — a server ``llm:``
   * block, or a ``routing.provider=external`` block.
   */
  smart_routing_enabled: boolean;
  /**
   * Which router backs Smart Routing on this server — the external Databricks
   * AI-Gateway ``task_v1`` router, the built-in judge, or both. Only the
   * external router needs the host's harness family on the gateway, so this is
   * what decides whether an off-gateway family can still be routed. A server
   * that predates the field reports neither, and the parser degrades from
   * ``smart_routing_enabled``.
   */
  smart_routing_sources: SmartRoutingSources;
  /**
   * Deployment-wide release features. Missing keys are disabled. The map is
   * the canonical gate for new frontend surfaces.
   */
  features: FeatureValues;
  /**
   * Compatibility field for servers/frontends predating ``features``.
   * New consumers should use :func:`isFeatureEnabled`.
   *
   * True when the server accepts UI-driven harness installs
   * (``harness_install`` in ``OMNIGENT_FEATURES``). Gates the New Chat dialog's
   * one-click "Install" action for a missing harness. Fails to ``false`` so a
   * failed probe never offers an install the server would reject.
   */
  harness_install_enabled: boolean;
  /**
   * Harness ids the install route accepts (bare ids + native spellings that
   * resolve to an npm-installable family, e.g. ``"codex"``, ``"codex-native"``).
   * The dialog offers the one-click install only for a harness in this set, so
   * it never shows an Install button the server would reject. Empty when
   * ``harness_install_enabled`` is false (or on a failed probe).
   */
  installable_harnesses: string[];
  /**
   * True when the server can transcribe dictation audio
   * (``WS /v1/dictation/stream``; the ``dictation`` extra plus models
   * are installed). Gates the composer mic button's server
   * speech-to-text fallback where the browser Web Speech API has no
   * backend (Electron, Firefox/Chromium).
   */
  dictation_available: boolean;
  /** Operator branding, or null when the built-in identity should be used. */
  branding?: Branding | null;
}

function parseBranding(raw: unknown): Branding | null {
  if (raw === null || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  const nonEmpty = (candidate: unknown): string | null =>
    typeof candidate === "string" && candidate.trim() !== "" ? candidate : null;
  const rawLogos =
    value.logos !== null && typeof value.logos === "object"
      ? (value.logos as Record<string, unknown>)
      : {};
  const logos = {
    main: nonEmpty(rawLogos.main),
    loading: nonEmpty(rawLogos.loading),
    favicon: nonEmpty(rawLogos.favicon),
  };
  const branding = {
    app_name: nonEmpty(value.app_name),
    heading: typeof value.heading === "string" ? value.heading : null,
    logos,
    powered_by: value.powered_by !== false,
  };
  const isEmpty =
    branding.app_name === null &&
    branding.heading === null &&
    logos.main === null &&
    logos.loading === null &&
    logos.favicon === null &&
    branding.powered_by;
  return isEmpty ? null : branding;
}

/** Sentinel used when the probe fails — accounts and release features are off. */
export const FALLBACK_SERVER_INFO: ServerInfo = {
  accounts_enabled: false,
  // Fail to multi-user: a failed probe must not hide account/sharing chrome.
  single_user: false,
  login_url: null,
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
  sandbox_providers: [],
  // Sharing fails OPEN (opposite of the other caps): a failed probe must
  // not silently disable sharing, so the sentinel is the permissive "on".
  sharing_mode: "on",
  public_sharing_enabled: true,
  server_version: null,
  smart_routing_enabled: false,
  smart_routing_sources: { external: false, oss: false },
  features: {},
  harness_install_enabled: false,
  installable_harnesses: [],
  dictation_available: false,
  branding: null,
};

/**
 * Read ``smart_routing_sources`` off the probe payload.
 *
 * Missing or non-object (a server that predates the field) degrades to
 * ``smart_routing_enabled`` on both keys: a server that can route is assumed
 * able to serve either source, so nothing is hidden on an older build.
 */
function parseSmartRoutingSources(raw: unknown, routingEnabled: boolean): SmartRoutingSources {
  if (typeof raw !== "object" || raw === null) {
    return { external: routingEnabled, oss: routingEnabled };
  }
  const sources = raw as Partial<Record<keyof SmartRoutingSources, unknown>>;
  return { external: sources.external === true, oss: sources.oss === true };
}

function parseFeatures(raw: unknown, harnessInstallEnabled: boolean): FeatureValues {
  const parsed: FeatureValues = {};
  if (typeof raw === "object" && raw !== null && !Array.isArray(raw)) {
    for (const [key, value] of Object.entries(raw)) {
      if (typeof value === "boolean") parsed[key] = value;
    }
  }
  // A server predating the feature map exposed this one release gate as a
  // top-level field. Preserve mixed-version behavior without making it a
  // second source for new features.
  if (!("harness_install" in parsed)) parsed.harness_install = harnessInstallEnabled;
  return parsed;
}

/** Return whether a known release feature is enabled; missing/loading is off. */
export function isFeatureEnabled(info: ServerInfo | "loading", feature: FeatureKey): boolean {
  return info !== "loading" && info.features?.[feature] === true;
}

let cachedServerInfo: ServerInfo | null = null;
let pendingServerInfo: Promise<ServerInfo> | null = null;

/**
 * Fetch ``/v1/info`` once and cache the result.
 *
 * Resolves to ``FALLBACK_SERVER_INFO`` on any failure (network error, non-JSON,
 * 5xx). The frontend treats "no probe result" as "accounts is
 * off" — failing closed prevents the accounts UI from rendering
 * against a server that doesn't actually support it.
 */
export async function resolveServerInfo(): Promise<ServerInfo> {
  if (cachedServerInfo !== null) return cachedServerInfo;
  if (pendingServerInfo !== null) return pendingServerInfo;
  pendingServerInfo = (async () => {
    try {
      // Route through the host transport (`hostFetch`) so the embed hits the
      // proxied omnigent API; standalone `hostFetch` falls back to plain
      // `fetch("/v1/info")`, preserving the original behavior.
      const res = await hostFetch("/v1/info");
      if (res.ok) {
        const data = (await res.json()) as Partial<ServerInfo>;
        const smartRoutingEnabled = data.smart_routing_enabled === true;
        const harnessInstallEnabled = data.harness_install_enabled === true;
        cachedServerInfo = {
          accounts_enabled: data.accounts_enabled === true,
          single_user: data.single_user === true,
          login_url: typeof data.login_url === "string" ? data.login_url : null,
          needs_setup: data.needs_setup === true,
          databricks_features: data.databricks_features === true,
          managed_sandboxes_enabled: data.managed_sandboxes_enabled === true,
          sandbox_provider:
            typeof data.sandbox_provider === "string" ? data.sandbox_provider : null,
          sandbox_providers: Array.isArray(data.sandbox_providers)
            ? data.sandbox_providers.filter((p): p is string => typeof p === "string")
            : [],
          sharing_mode: SHARING_MODES.includes(data.sharing_mode as SharingMode)
            ? (data.sharing_mode as SharingMode)
            : "on",
          // Fail open: only an explicit false disables the public toggle.
          public_sharing_enabled: data.public_sharing_enabled !== false,
          server_version: typeof data.server_version === "string" ? data.server_version : null,
          smart_routing_enabled: smartRoutingEnabled,
          smart_routing_sources: parseSmartRoutingSources(
            data.smart_routing_sources,
            smartRoutingEnabled,
          ),
          features: parseFeatures(data.features, harnessInstallEnabled),
          harness_install_enabled: harnessInstallEnabled,
          installable_harnesses: Array.isArray(data.installable_harnesses)
            ? data.installable_harnesses.filter((h): h is string => typeof h === "string")
            : [],
          dictation_available: data.dictation_available === true,
          branding: parseBranding(data.branding),
        };
        return cachedServerInfo;
      }
    } catch {
      // Network failure — fall through to the off sentinel.
    }
    cachedServerInfo = FALLBACK_SERVER_INFO;
    return cachedServerInfo;
  })();
  return pendingServerInfo;
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
  return cachedServerInfo;
}

/**
 * Whether the server is an explicit single-user local runtime, per the
 * server's ``single_user`` signal (``OMNIGENT_LOCAL_SINGLE_USER=1``). This
 * is NOT the same as "no accounts / no login" — a multi-user header-auth
 * deploy (SSO proxy injecting ``X-Forwarded-Email``) also reports
 * ``accounts_enabled: false`` / ``login_url: null`` but is genuinely
 * multi-user, so it must keep its account/sharing chrome. Returns ``false``
 * while the probe is still loading (and on the failed-probe sentinel).
 */
export function isSingleUserMode(info: ServerInfo | "loading"): boolean {
  return info !== "loading" && info.single_user;
}

/**
 * Known provider id → display name for the sandbox label. Providers
 * not listed here fall back to a title-cased id so a newly-wired
 * provider still reads sensibly without a frontend change.
 */
const SANDBOX_PROVIDER_NAMES: Record<string, string> = {
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
    SANDBOX_PROVIDER_NAMES[provider] ?? provider.charAt(0).toUpperCase() + provider.slice(1);
  return `${name} Sandbox`;
}

/**
 * Provider ids to offer as new-session sandbox rows.
 *
 * Falls back to the single ``sandbox_provider`` when the server reports
 * no list; ``[null]`` yields one row with the generic label. Tolerates a
 * missing list (a hand-built ServerInfo) rather than throwing on render.
 */
export function sandboxProviderOptions(info: ServerInfo): (string | null)[] {
  const offered = info.sandbox_providers;
  if (Array.isArray(offered) && offered.length > 0) return offered;
  return [info.sandbox_provider];
}
