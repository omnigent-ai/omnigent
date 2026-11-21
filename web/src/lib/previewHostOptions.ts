import { fetchHosts } from "@/hooks/useHosts";
import { inferHostSshTarget, normalizeSshTarget } from "./editorLinks";
import { getDesktopFeatures, navigationHostOptions, type BrowserHostOptions } from "./nativeBridge";

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
    if (!hostId || !isLoopbackPreviewUrl(url)) return options;
    if (!(await getDesktopFeatures())?.sshLocalhostPreview) return options;
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
