import { useCallback, useEffect, useRef, useState } from "react";
import { authorizeRegistryService, registryRequest, type RegistryService } from "@/lib/mcpRegistry";
import { readLastMcpServices, writeLastMcpServices } from "@/lib/mcpPreferences";

export function useMcpRegistryCatalog(enabled = true) {
  const [services, setServices] = useState<RegistryService[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Re-enabling means a new open: mark the catalog stale during this render, before
  // anything can read the previous open's services as current.
  const [reading, setReading] = useState(enabled);
  if (enabled !== reading) {
    setReading(enabled);
    if (enabled) setLoading(true);
  }
  const request = useRef(0);
  const refresh = useCallback(async () => {
    // A focus refresh and a post-sign-in refresh can overlap; the newest read wins.
    const id = ++request.current;
    try {
      const entries: RegistryService[] = [];
      let cursor: string | null = null;
      do {
        const result: { data: RegistryService[]; next_cursor?: string | null } =
          // eslint-disable-next-line no-await-in-loop -- Each page needs the previous cursor.
          await registryRequest(cursor ? `?after=${encodeURIComponent(cursor)}` : "");
        entries.push(...result.data);
        cursor = result.next_cursor ?? null;
      } while (cursor);
      if (id === request.current) {
        setServices(entries);
        setError(null);
      }
      return entries;
    } catch (e) {
      if (id === request.current) {
        setError(e instanceof Error ? e.message : "Could not load MCP services");
      }
      return undefined;
    } finally {
      if (id === request.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    if (!enabled) return;
    void refresh();
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [enabled, refresh]);
  return { services, error, loading, refresh };
}

/** Keep preferences intact until an explicit toggle; filter only the launch payload. */
export function useMcpRegistrySelection(draft: string[] | undefined, enabled: boolean) {
  const [selected, setSelected] = useState(() => draft ?? readLastMcpServices());
  const catalog = useMcpRegistryCatalog(enabled);
  const toggle = (id: string, next: boolean) => {
    setSelected((current) => {
      const updated = next ? [...new Set([...current, id])] : current.filter((s) => s !== id);
      writeLastMcpServices(updated);
      return updated;
    });
  };
  const selectionError =
    enabled && selected.length > 0
      ? catalog.loading
        ? "Loading MCP services…"
        : catalog.error
          ? "Could not load MCP services. Open MCPs to retry or turn them off."
          : null
      : null;
  return {
    selected,
    toggle,
    catalog,
    selectionError,
    servicesForLaunch: enabled
      ? selected.filter((id) => catalog.services.some((service) => service.id === id))
      : [],
  };
}

/** Sign-in failure for one service, kept apart from the catalog's own load error. */
export interface RegistrySignInError {
  id: string;
  message: string;
}

/** One abortable registry sign-in, confirmed by re-reading the catalog. */
export function useRegistrySignIn(refresh: () => Promise<RegistryService[] | undefined>) {
  const [authorizing, setAuthorizing] = useState<string | null>(null);
  const [error, setError] = useState<RegistrySignInError | null>(null);
  const authorization = useRef<AbortController | null>(null);
  useEffect(() => () => authorization.current?.abort(), []);
  const signIn = useCallback(
    async (service: RegistryService, workspaceUrl?: string): Promise<boolean> => {
      authorization.current?.abort();
      const controller = new AbortController();
      authorization.current = controller;
      setAuthorizing(service.id);
      setError(null);
      try {
        await authorizeRegistryService(service, controller.signal, workspaceUrl);
        const updated = await refresh();
        // A failed re-read is not the same as a sign-in that did not take.
        if (updated === undefined) {
          throw new Error("Couldn’t confirm the connection. Please try again.");
        }
        if (!updated.find((s) => s.id === service.id)?.connected) {
          throw new Error("Account is not connected. Please try again.");
        }
        return !controller.signal.aborted;
      } catch (e) {
        if (!controller.signal.aborted) {
          setError({ id: service.id, message: e instanceof Error ? e.message : "Sign-in failed" });
        }
        return false;
      } finally {
        if (!controller.signal.aborted) setAuthorizing(null);
      }
    },
    [refresh],
  );
  const clearError = useCallback(() => setError(null), []);
  return { signIn, authorizing, error, clearError };
}
