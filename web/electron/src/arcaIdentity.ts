import { normalizeSavedServerUrl, databricksWorkspaceUiUrl } from "./url.ts";

export const IDENTITY_START = "__OMNIGENT_ARCA_IDENTITY_START__";
export const IDENTITY_END = "__OMNIGENT_ARCA_IDENTITY_END__";

/** The host daemon a connect run started, bound to the selected server target. */
export interface ArcaIdentity {
  serverUrl: string;
  hostId: string;
}

/** A connect run's result, as far as the identity store reads it. */
export interface ArcaIdentityRunResult {
  ok: boolean;
  identity?: ArcaIdentity | null | undefined;
}

/** A browser view's agent context, as far as the identity check reads it. */
export interface ArcaAgentContext {
  serverTarget?: string | null | undefined;
  sourceHostId?: unknown;
}

/** Live gates for an agent context, re-read on every check. */
export interface ArcaAgentContextGates {
  enabled: unknown;
  managed: unknown;
  serverTarget: unknown;
}

/** Fields of parsed status JSON; reading one throws on null or undefined, as property access does. */
function fieldsOf(value: unknown): Record<string, unknown> {
  return value as Record<string, unknown>;
}

/** Canonical selected target, preserving mount and workspace selector. */
export function arcaTarget(raw: unknown): string | null {
  try {
    const saved = normalizeSavedServerUrl(raw);
    // The constructor stringifies any value itself; the cast only lets TypeScript pass it.
    const url = new URL((databricksWorkspaceUiUrl(saved) ?? saved) as string);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    url.hash = "";
    url.pathname = url.pathname.replace(/\/+$/, "") || "/";
    url.searchParams.sort();
    return url.href;
  } catch {
    return null;
  }
}

/** Read only the bounded, machine-readable status from the Arca connect run. */
export function readArcaIdentity(
  output: string,
  serverUrl: unknown,
  onUnavailable: (reason: string) => void = () => {},
): ArcaIdentity | null {
  const unavailable = (reason: string): null => {
    onUnavailable(reason);
    return null;
  };
  const target = arcaTarget(serverUrl);
  const start = output.lastIndexOf(IDENTITY_START);
  const end = output.indexOf(IDENTITY_END, start);
  if (!target) return unavailable("invalid selected target");
  if (start < 0 || end < 0) return unavailable("status markers missing");
  try {
    const body = fieldsOf(JSON.parse(output.slice(start + IDENTITY_START.length, end)));
    if (!Array.isArray(body.daemons) || body.daemons.length !== 1) {
      return unavailable("status must contain exactly one daemon");
    }
    const daemon = fieldsOf(body.daemons[0]);
    // CLI status normalizes the UI mount to the API mount and drops ?o=.
    // The selected workspace remains bound by the command's --server argument.
    const expected = new URL(target);
    const requestedOrg = expected.searchParams.get("o");
    expected.search = "";
    // A daemon target may carry ?o=: the requested one, or the one sign-in
    // resolved when none was requested. Another workspace's never matches.
    const daemonTarget = (() => {
      const raw = arcaTarget(daemon.target);
      if (!raw) return null;
      const url = new URL(raw);
      const org = url.searchParams.get("o");
      if (org !== null && requestedOrg !== null && org !== requestedOrg) return null;
      url.search = "";
      return url.href;
    })();
    // The CLI reuses a daemon on another URL for the same workspace and names
    // the requested target in same_workspace_as.
    const servesTarget =
      (daemonTarget === expected.href && arcaTarget(daemon.server_url) === expected.href) ||
      (typeof daemon.same_workspace_as === "string" &&
        arcaTarget(daemon.same_workspace_as) === expected.href);
    if (
      daemon.mode !== "server" ||
      daemon.process !== "online" ||
      daemon.host_status !== "online" ||
      daemon.error != null ||
      !servesTarget ||
      typeof daemon.host_id !== "string" ||
      !/^[a-f0-9]{32}$/i.test(daemon.host_id)
    ) {
      return unavailable("daemon status or target not eligible");
    }
    return { serverUrl: target, hostId: daemon.host_id.toLowerCase() };
  } catch {
    return unavailable("status could not be parsed");
  }
}

/** Ends a run begun with `begin`; returns the result it was given. */
export type FinishArcaIdentityRun = <Result extends ArcaIdentityRunResult>(
  result: Result,
) => Result;

export interface ArcaIdentityStore {
  begin: (serverUrl: unknown) => FinishArcaIdentityRun;
  get: (serverUrl: unknown) => ArcaIdentity | null;
}

/** Main-owned identity cache; new/failed/unrecognized runs revoke older identity. */
export function createArcaIdentityStore(): ArcaIdentityStore {
  const runs = new Map<string, { identity: ArcaIdentity | null }>();
  return {
    begin(serverUrl) {
      const target = arcaTarget(serverUrl);
      const run: { identity: ArcaIdentity | null } = { identity: null };
      if (target) runs.set(target, run);
      return (result) => {
        if (target && runs.get(target) === run) {
          run.identity =
            result.ok && result.identity?.serverUrl === target ? result.identity : null;
        }
        return result;
      };
    },
    get(serverUrl) {
      const target = arcaTarget(serverUrl);
      return (target === null ? undefined : runs.get(target))?.identity ?? null;
    },
  };
}

export function isArcaAgentContext(
  identity: ArcaIdentity | null | undefined,
  context: ArcaAgentContext | null | undefined,
  { enabled, managed, serverTarget }: ArcaAgentContextGates,
): boolean {
  return (
    enabled === true &&
    managed === true &&
    identity != null &&
    context != null &&
    identity.serverUrl === arcaTarget(serverTarget) &&
    context.serverTarget === identity.serverUrl &&
    typeof context.sourceHostId === "string" &&
    context.sourceHostId === identity.hostId
  );
}
