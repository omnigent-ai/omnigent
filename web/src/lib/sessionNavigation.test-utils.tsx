import { useCallback, type ReactNode } from "react";
import { useLocation } from "./routing";
import { SessionNavigationProvider, type SessionHrefResolver } from "./sessionNavigation";

/** Test host that supplies its current query to an injected destination policy. */
export function SessionNavigationTestHost({
  resolveHref,
  children,
}: {
  resolveHref: SessionHrefResolver;
  children: ReactNode;
}) {
  const { search } = useLocation();
  const resolve = useCallback(
    (sessionId: string | null) => resolveHref(sessionId, search),
    [resolveHref, search],
  );
  return <SessionNavigationProvider resolveHref={resolve}>{children}</SessionNavigationProvider>;
}
