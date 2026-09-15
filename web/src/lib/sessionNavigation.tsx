import {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import type { NavigateOptions } from "react-router-dom";
import { useNavigate } from "@/lib/routing";
import { sessionPageHref } from "./sessionLinks";

export { sessionPageHref } from "./sessionLinks";

/** Hosts choose destinations for selected sessions; null clears the selection. */
export type SessionHrefResolver = (sessionId: string | null, search?: string) => string;
interface SessionNavigation {
  href: SessionHrefResolver;
  latestHref: SessionHrefResolver;
}
const SessionNavigationContext = createContext<SessionNavigation | null>(null);

/** Render links in the current session host; global navigation and sharing stay canonical. */
export function useSessionHref(): SessionHrefResolver {
  return useContext(SessionNavigationContext)?.href ?? sessionPageHref;
}

/** Resolve at navigation time so async completions preserve the latest host query state. */
export function useNavigateToSession() {
  const href = useContext(SessionNavigationContext)?.latestHref ?? sessionPageHref;
  const navigate = useNavigate();
  return useCallback(
    (sessionId: string | null, options?: NavigateOptions) => {
      const to = href(sessionId);
      return options === undefined ? navigate(to) : navigate(to, options);
    },
    [navigate, href],
  );
}

/** Override session-local destinations without changing global navigation or routing. */
export function SessionNavigationProvider({
  resolveHref,
  children,
}: {
  resolveHref: SessionHrefResolver;
  children: ReactNode;
}) {
  const resolverRef = useRef(resolveHref);
  useLayoutEffect(() => {
    resolverRef.current = resolveHref;
  }, [resolveHref]);
  const latestHref = useCallback(
    (sessionId: string | null, search?: string) => resolverRef.current(sessionId, search),
    [],
  );
  const value = useMemo(() => ({ href: resolveHref, latestHref }), [resolveHref, latestHref]);
  return (
    <SessionNavigationContext.Provider value={value}>{children}</SessionNavigationContext.Provider>
  );
}
