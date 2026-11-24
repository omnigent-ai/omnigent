import { fetchHosts } from "@/hooks/useHosts";
import { inferHostSshTarget, normalizeSshTarget } from "./editorLinks";
import {
  getDesktopFeatures,
  navigationHostOptions,
  SSH_PREVIEW_PROTOCOL,
  TRUSTED_REMOTE_PREVIEW_PROTOCOL,
  type BrowserHostOptions,
  type BrowserNavigationResult,
} from "./nativeBridge";
import { fetchSessionHostId } from "./fetchSessionHostId";

export function isLoopbackPreviewUrl(url: string): boolean {
  try {
    const { hostname, protocol } = new URL(url);
    return (
      ["http:", "https:"].includes(protocol) &&
      (hostname === "localhost" ||
        hostname.endsWith(".localhost") ||
        hostname === "127.0.0.1" ||
        hostname === "[::1]")
    );
  } catch {
    return false;
  }
}

/** An SSH suggestion is only a route hint: the desktop asks consent and proves the identity. */
export async function previewHostOptions(
  hostId: string | null,
  url: string,
): Promise<BrowserHostOptions> {
  const options = navigationHostOptions(hostId);
  try {
    if (!hostId) return options;
    const features = await getDesktopFeatures();
    if (features?.trustedRemoteLocalhostPreview) {
      options.trustedRemotePreviewProtocol = TRUSTED_REMOTE_PREVIEW_PROTOCOL;
    }
    if (!features?.sshLocalhostPreview) return options;
    options.sshPreviewProtocol = SSH_PREVIEW_PROTOCOL;
    if (!isLoopbackPreviewUrl(url)) return options;
    // Discovery only adds a hint; a stalled lookup must not hold up navigation.
    const hosts = await fetchHosts(true, undefined, AbortSignal.timeout(1_000));
    const host = hosts.find((candidate) => candidate.host_id === hostId);
    if (host?.status !== "online") return options;
    let target = inferHostSshTarget(host);
    if (!target && !host.sandbox_provider) target = normalizeSshTarget(host.name);
    return target ? { ...options, sourceSshTarget: target } : options;
  } catch {
    return options;
  }
}

/** Consent does not authorize a session that moved hosts while the question was open. */
export async function withPreviewHostRetry(
  sessionId: string,
  sourceHostId: string | null,
  navigate: (
    retryOptions?: Pick<BrowserHostOptions, "navigationRetryToken">,
  ) => Promise<BrowserNavigationResult | null | undefined>,
): Promise<BrowserNavigationResult | null | undefined> {
  const result = await navigate();
  if (result?.retryWithFreshHost !== true) return result;
  if (typeof result.navigationRetryToken !== "string" || !result.navigationRetryToken) {
    return {
      ...result,
      ok: false,
      retryWithFreshHost: false,
      error: "The desktop could not preserve this navigation. Retry the preview.",
    };
  }
  if (!sourceHostId || (await fetchSessionHostId(sessionId, { fresh: true })) !== sourceHostId) {
    return {
      ...result,
      ok: false,
      retryWithFreshHost: false,
      error: "The session host changed while allowing localhost access. Retry the preview.",
    };
  }
  return navigate({ navigationRetryToken: result.navigationRetryToken });
}
