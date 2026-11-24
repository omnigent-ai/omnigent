import { withBasePath } from "@/lib/basePath";
import { getOmnigentTransformShareLink } from "@/lib/host";

function fileShareParams(filePath: string): URLSearchParams {
  return new URLSearchParams({ file: filePath, fullscreen: "1" });
}

export function getSessionSharePath(sessionId: string, filePath?: string): string {
  const path = `/c/${sessionId}`;
  return filePath ? `${path}?${fileShareParams(filePath)}` : path;
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
  const fallbackLink = `${window.location.origin}${withBasePath(path)}`;
  const link = transform ? transform(path) : fallbackLink;
  if (!filePath) return link;

  let url: URL;
  try {
    url = new URL(link, window.location.origin);
  } catch {
    url = new URL(fallbackLink);
  }
  const target = url.hash.startsWith("#/") ? new URL(url.hash.slice(1), url.origin) : url;
  for (const [key, value] of fileShareParams(filePath)) target.searchParams.set(key, value);
  if (target !== url) url.hash = `${target.pathname}${target.search}${target.hash}`;
  return url.toString();
}

/**
 * The absolute URL of a project's home, resolved like `getShareableSessionLink`
 * so embedded hosts and base-path deployments get a link others can open.
 */
export function getShareableProjectLink(
  projectName: string,
  rebasePath: (path: string) => string,
): string {
  const path = rebasePath(`/projects/${encodeURIComponent(projectName)}`);
  const transform = getOmnigentTransformShareLink();
  return transform ? transform(path) : `${window.location.origin}${withBasePath(path)}`;
}
