import { fetchHosts } from "@/hooks/useHosts";
import { inferHostSshTarget, normalizeSshTarget } from "./editorLinks";
import { getDesktopFeatures, navigationHostOptions, type BrowserHostOptions } from "./nativeBridge";

/** An SSH suggestion is only a route hint: the desktop asks consent and proves the identity. */
export async function previewHostOptions(
  hostId: string | null,
  url: string,
): Promise<BrowserHostOptions> {
  const options = navigationHostOptions(hostId);
  try {
    const { hostname, protocol } = new URL(url);
    if (
      !hostId ||
      !["http:", "https:"].includes(protocol) ||
      !(
        hostname === "localhost" ||
        hostname.endsWith(".localhost") ||
        hostname === "127.0.0.1" ||
        hostname === "[::1]"
      )
    )
      return options;
    if (!(await getDesktopFeatures())?.sshLocalhostPreview) return options;
    const host = (await fetchHosts(true)).find((candidate) => candidate.host_id === hostId);
    const target =
      host?.status === "online"
        ? (inferHostSshTarget(host) ??
          (!host.sandbox_provider ? normalizeSshTarget(host.name) : null))
        : null;
    return target ? { ...options, sourceSshTarget: target } : options;
  } catch {
    return options;
  }
}
