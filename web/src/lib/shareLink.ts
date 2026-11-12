import { withBasePath } from "@/lib/basePath";
import { getOmnigentTransformShareLink } from "@/lib/host";

export function getSessionSharePath(sessionId: string, filePath?: string): string {
  const path = `/c/${sessionId}`;
  return filePath ? `${path}?${new URLSearchParams({ file: filePath, fullscreen: "1" })}` : path;
}

/**
 * The absolute URL people outside Omnigent use to open a session.
 *
 * The basename-rebased session path goes through the host's
 * `transformShareLink` in the embed, which returns the full URL (its own
 * origin, routing, and query). Standalone has no transform, so the deployment
 * base path (e.g. `/proxy/6767`) and the page's origin are added here.
 *
 * @param sessionId - The session to link, e.g. `"conv_123"`.
 * @param rebasePath - The active `useRebasePath()` primitive.
 * @param filePath - Optional workspace file to open in full screen.
 */
export function getShareableSessionLink(
  sessionId: string,
  rebasePath: (path: string) => string,
  filePath?: string,
): string {
  const path = rebasePath(getSessionSharePath(sessionId));
  const transform = getOmnigentTransformShareLink();
  const link = transform ? transform(path) : `${window.location.origin}${withBasePath(path)}`;
  if (!filePath) return link;

  const url = new URL(link);
  const target = url.hash.startsWith("#/") ? new URL(url.hash.slice(1), url.origin) : url;
  target.searchParams.set("file", filePath);
  target.searchParams.set("fullscreen", "1");
  if (target !== url) url.hash = `${target.pathname}${target.search}${target.hash}`;
  return url.toString();
}
