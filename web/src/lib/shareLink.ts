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
  return shareableLink(path, filePath ? fileShareParams(filePath) : undefined);
}

/**
 * The absolute URL of a project's home, resolved like `getShareableSessionLink`
 * so embedded hosts and base-path deployments get a link others can open.
 */
export function getShareableProjectLink(
  projectName: string,
  rebasePath: (path: string) => string,
): string {
  return shareableLink(rebasePath(projectPath(projectName)));
}

/** The absolute URL that opens a project's board with one ticket's detail panel. */
export function getShareableTicketLink(
  projectName: string,
  ticketId: string,
  rebasePath: (path: string) => string,
): string {
  return shareableLink(
    rebasePath(projectPath(projectName)),
    new URLSearchParams({ ticket: ticketId }),
  );
}

function projectPath(projectName: string): string {
  return `/projects/${encodeURIComponent(projectName)}`;
}

/**
 * Turns a rebased in-app path into an absolute URL through the host's
 * `transformShareLink`, or the page origin and deployment base path standalone.
 * `params` go on the in-app route, inside a hash route when the host uses one.
 */
function shareableLink(path: string, params?: URLSearchParams): string {
  const transform = getOmnigentTransformShareLink();
  const fallbackLink = `${window.location.origin}${withBasePath(path)}`;
  const link = transform ? transform(path) : fallbackLink;
  if (!params) return link;

  let url: URL;
  try {
    url = new URL(link, window.location.origin);
  } catch {
    url = new URL(fallbackLink);
  }
  const target = url.hash.startsWith("#/") ? new URL(url.hash.slice(1), url.origin) : url;
  for (const [key, value] of params) target.searchParams.set(key, value);
  if (target !== url) url.hash = `${target.pathname}${target.search}${target.hash}`;
  return url.toString();
}
