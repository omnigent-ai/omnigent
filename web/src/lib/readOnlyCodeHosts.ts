// Pull requests on a registered named host that's read-only from this app: every change names
// the host itself as the place to make it.

import { namedCodeHost } from "@/lib/codeHosts";
import type { NamedCodeHost } from "@/lib/pullRequestIntegrations";

function readOnlyHostOf(url: string): NamedCodeHost | null {
  try {
    const named = namedCodeHost(new URL(url).host);
    return named?.readOnly ? named : null;
  } catch {
    return null;
  }
}

/** Whether `url` is a pull request on a host this app can't change. */
export function isReadOnlyHostPullRequest(url: string): boolean {
  return readOnlyHostOf(url) !== null;
}

/** The reason no change from this Mac applies to `url`, or null where one may. */
export function readOnlyHostWriteReason(url: string): string | null {
  const named = readOnlyHostOf(url);
  return named ? `Change this pull request on ${named.copy.label}.` : null;
}
