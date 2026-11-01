import { withBasePath } from "@/lib/basePath";
import { getOmnigentTransformShareLink } from "@/lib/host";

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
 */
export function getShareableSessionLink(
  sessionId: string,
  rebasePath: (path: string) => string,
): string {
  const path = rebasePath(`/c/${sessionId}`);
  const transform = getOmnigentTransformShareLink();
  return transform ? transform(path) : `${window.location.origin}${withBasePath(path)}`;
}
