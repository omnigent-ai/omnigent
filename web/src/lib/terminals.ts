/** UI-facing terminal record returned by the session resources API. */
export interface TerminalInfo {
  /** Opaque, stable resource id used for addressing and tab keys. */
  id: string;
  /** Terminal name from the spec, e.g. ``"bash"``. */
  name: string;
  /** Session key, e.g. ``"s1"``. */
  session: string;
  /** Whether the underlying tmux session is currently running. */
  running: boolean;
  /**
   * Loopback attach URL advertised by the session's runner, when the
   * server disclosed one (owner-level callers only). Lets a browser on
   * the same machine as the runner attach with zero relay legs; the
   * view probes it and silently falls back to the relay when
   * unreachable (other machines, Safari, listener down).
   */
  directAttachUrl?: string;
}

/**
 * Validate a server-provided direct-attach URL.
 *
 * Only loopback ``ws://127.0.0.1:...`` URLs are honored — anything else
 * (including ``wss://``, hostnames, or non-string junk) is dropped so a
 * misbehaving server can never steer the terminal WebSocket at an
 * arbitrary host.
 */
export function parseDirectAttachUrl(value: unknown): string | undefined {
  if (typeof value !== "string" || !value.startsWith("ws://127.0.0.1:")) return undefined;
  return value;
}

/**
 * Append the per-attach params to a direct-attach URL.
 *
 * The server-provided URL always carries ``?token=...``, so extra
 * params join with ``&``. Mirrors ``buildAttachPath``'s param policy:
 * only emit what is set.
 */
export function withAttachParams(directAttachUrl: string, readOnly: boolean): string {
  const params = new URLSearchParams();
  if (readOnly) params.set("read_only", "true");
  const qs = params.toString();
  return qs ? `${directAttachUrl}&${qs}` : directAttachUrl;
}

/**
 * Probe budget when the local-network permission is already granted (or
 * the browser needs none): a live loopback listener answers in
 * single-digit milliseconds, so this only pads for scheduling jitter.
 */
export const DIRECT_PROBE_TIMEOUT_MS = 1_500;
/**
 * Probe budget while the browser is showing its Local Network Access
 * prompt. Chrome parks the WebSocket in CONNECTING until the user
 * decides, so this probe deliberately outwaits a human: it either opens
 * (Allow), errors (Block / nothing listening), or times out (prompt
 * ignored). It runs in the background behind a live relay connection,
 * so patience here costs the user nothing.
 */
export const DIRECT_UPGRADE_TIMEOUT_MS = 120_000;

/** Probe URLs that completed a handshake this page lifetime. */
const directKnownGood = new Set<string>();

/** Derive the listener's side-effect-free ``/probe`` URL from an attach URL. */
export function directProbeUrl(directAttachUrl: string): string | null {
  let url: URL;
  try {
    url = new URL(directAttachUrl);
  } catch {
    return null;
  }
  return `${url.protocol}//${url.host}/probe${url.search}`;
}

/**
 * Open a WebSocket to *probeUrl* and report whether the handshake
 * completed within *timeoutMs*.
 *
 * WebSocket handshakes are exempt from CORS, so the failure modes are
 * "nothing listening", an auth rejection, or a browser policy block
 * (Safari's mixed-content rule; Chrome's Local Network Access denial) —
 * all of which resolve ``false`` and leave the caller on the relay
 * path. While Chrome's LNA prompt is showing, the socket sits in
 * CONNECTING; the caller picks *timeoutMs* to either give up fast
 * (permission granted, listener should answer instantly) or outwait the
 * prompt. Never throws. An aborted *signal* settles ``false``.
 */
export function probeDirectAttach(
  probeUrl: string,
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<boolean> {
  return new Promise((resolve) => {
    let ws: WebSocket;
    try {
      // Safari throws a synchronous SecurityError for ws:// from an
      // https page; treat it as an ordinary probe failure.
      ws = new WebSocket(probeUrl);
    } catch {
      resolve(false);
      return;
    }
    let settled = false;
    const settle = (ok: boolean) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      try {
        ws.close();
      } catch {
        /* noop */
      }
      resolve(ok);
    };
    const onAbort = () => settle(false);
    const timer = window.setTimeout(() => settle(false), timeoutMs);
    if (signal?.aborted) {
      onAbort();
      return;
    }
    signal?.addEventListener("abort", onAbort);
    ws.addEventListener("open", () => settle(true));
    ws.addEventListener("error", () => settle(false));
    ws.addEventListener("close", () => settle(false));
  });
}

/**
 * Browser Local Network Access permission state for loopback dials,
 * or ``"unknown"`` where the Permissions API can't answer (Firefox,
 * Safari, older Chrome, or a query-name the browser doesn't know).
 */
export type LocalNetworkPermission = "granted" | "denied" | "prompt" | "unknown";

/**
 * Query Chrome's Local Network Access permission without triggering a
 * prompt. Best-effort: any Permissions API absence or rejection maps to
 * ``"unknown"`` so callers fall back to probing behavior.
 */
export async function queryLocalNetworkPermission(): Promise<LocalNetworkPermission> {
  try {
    const permissions = (
      globalThis.navigator as Navigator & {
        permissions?: { query: (desc: { name: string }) => Promise<{ state: string }> };
      }
    ).permissions;
    if (!permissions?.query) return "unknown";
    const status = await permissions.query({ name: "local-network-access" });
    if (status.state === "granted" || status.state === "denied" || status.state === "prompt") {
      return status.state;
    }
    return "unknown";
  } catch {
    return "unknown";
  }
}

/**
 * Pick the URL for the *initial* terminal connection, never keeping the
 * user waiting on a permission prompt: dial direct only when the
 * loopback listener is already known-reachable (a prior probe
 * succeeded, or the local-network permission is granted and the
 * listener answers a quick probe). Everything else connects via the
 * relay immediately — {@link watchDirectUpgrade} then negotiates the
 * upgrade in the background.
 */
export async function resolveInitialAttachUrl(
  directUrl: string | undefined,
  relayUrl: string,
): Promise<string> {
  if (!directUrl) return relayUrl;
  const probeUrl = directProbeUrl(directUrl);
  if (probeUrl === null) return relayUrl;
  if (directKnownGood.has(probeUrl)) {
    if (await probeDirectAttach(probeUrl, DIRECT_PROBE_TIMEOUT_MS)) return directUrl;
    directKnownGood.delete(probeUrl);
    return relayUrl;
  }
  if ((await queryLocalNetworkPermission()) === "granted") {
    if (await probeDirectAttach(probeUrl, DIRECT_PROBE_TIMEOUT_MS)) {
      directKnownGood.add(probeUrl);
      return directUrl;
    }
  }
  return relayUrl;
}

/**
 * Background half of the direct upgrade: patiently probe the loopback
 * listener so Chrome raises its Local Network Access prompt, and report
 * whether the direct path became usable. ``true`` means the handshake
 * completed (the user allowed access, or no permission was needed) and
 * the URL is cached known-good — the caller should re-dial the terminal
 * via {@link resolveInitialAttachUrl}. ``false`` means denied, blocked,
 * unreachable, timed out, or aborted: stay on the relay.
 */
export async function watchDirectUpgrade(
  directUrl: string,
  signal?: AbortSignal,
  timeoutMs: number = DIRECT_UPGRADE_TIMEOUT_MS,
): Promise<boolean> {
  const probeUrl = directProbeUrl(directUrl);
  if (probeUrl === null) return false;
  if ((await queryLocalNetworkPermission()) === "denied") return false;
  if (await probeDirectAttach(probeUrl, timeoutMs, signal)) {
    directKnownGood.add(probeUrl);
    return true;
  }
  return false;
}

/** Test hook: forget known-good listeners. */
export function clearDirectAttachCaches(): void {
  directKnownGood.clear();
}

/** TanStack Query key for a conversation's terminal resources. */
export function terminalsQueryKey(conversationId: string): readonly unknown[] {
  return ["conversation", conversationId, "terminals"];
}

/** Convert a terminal resource wire payload into the UI-facing record. */
export function terminalInfoFromResource(resource: Record<string, unknown>): TerminalInfo | null {
  const id = resource.id;
  if (typeof id !== "string" || !id) return null;
  const rawMetadata = resource.metadata;
  const metadata =
    rawMetadata && typeof rawMetadata === "object" && !Array.isArray(rawMetadata)
      ? (rawMetadata as Record<string, unknown>)
      : {};
  const terminalName = metadata.terminal_name;
  const sessionKey = metadata.session_key;
  const running = metadata.running;
  const fallbackName = resource.name;
  return {
    id,
    name:
      typeof terminalName === "string" && terminalName
        ? terminalName
        : typeof fallbackName === "string"
          ? fallbackName
          : "",
    session: typeof sessionKey === "string" ? sessionKey : "",
    running: typeof running === "boolean" ? running : false,
    directAttachUrl: parseDirectAttachUrl(metadata.direct_attach_url),
  };
}
