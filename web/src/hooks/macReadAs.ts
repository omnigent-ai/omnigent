// The login a pull request view reads GitHub as where the Pull Requests page named one of this
// Mac's accounts; null everywhere else, so session panels keep reading as the host's account.

import { createContext, useContext } from "react";

import { macViewerLogin, useDesktopReadPathVersion } from "@/lib/desktopReadPath";
import type { MacNamedReader } from "@/lib/macNamedReads";
import { useMacReadScopeVersion } from "@/lib/macReadScope";

/**
 * Provided only by the Pull Requests page: the login it reads as by name, or null under
 * Automatic. Absent (null) outside the page.
 */
export const MacReadAsContext = createContext<{ reader: MacNamedReader | null } | null>(null);

/** The page's named reader, when it reads `host` (any host when absent); else null. */
export function useMacReadAs(host?: string | null): MacNamedReader | null {
  const reader = useContext(MacReadAsContext)?.reader ?? null;
  if (!reader) return null;
  if (host === undefined) return reader;
  return host && host.toLowerCase() === reader.host.toLowerCase() ? reader : null;
}

/** Whether this view is inside the Pull Requests page, whose account choice is its own. */
export function useInPullRequestsPage(): boolean {
  return useContext(MacReadAsContext) !== null;
}

/** {@link macViewerLogin} for `url` in this view, re-read as the account or named reader changes. */
export function useMacViewerLogin(url: string | null | undefined): string | null {
  const readAs = useMacReadAs();
  useDesktopReadPathVersion();
  useMacReadScopeVersion();
  return macViewerLogin(url, readAs);
}
