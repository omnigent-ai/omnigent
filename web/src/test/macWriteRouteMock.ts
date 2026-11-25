// A stand-in for this Mac's write route in tests that mock `@/lib/desktopPullRequestWrites`:
// the route helpers that check it again resolve the mocked route too, as the real ones do.

import type * as DesktopPullRequestWrites from "@/lib/desktopPullRequestWrites";
import { sameNamedReader } from "@/lib/macNamedReads";

type Writes = typeof DesktopPullRequestWrites;

/** `macWriteRoute` as `route` answers it, with `macRouteCurrent` and `macRouteAs` to match. */
export function mockedMacRoute(
  actual: Writes,
  route: (url: string) => ReturnType<Writes["macWriteRoute"]>,
): Pick<Writes, "macWriteRoute" | "macRouteCurrent" | "macRouteAs"> {
  return {
    macWriteRoute: (url) => route(url ?? ""),
    macRouteCurrent: (url, sent) => {
      const current = route(url);
      return actual.sameMacRoute(current, sent) ? current : null;
    },
    macRouteAs: (url, madeAs) => {
      const current = route(url);
      const sameLogin = current?.actor.login.toLowerCase() === madeAs.login.toLowerCase();
      return current && sameLogin && sameNamedReader(current.reader, madeAs.reader)
        ? current
        : null;
    },
  };
}
