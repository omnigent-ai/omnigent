import { useCallback, useEffect, useState } from "react";
import { registryRequest, type RegistryService } from "@/lib/mcpRegistry";
import { readLastMcpServices, writeLastMcpServices } from "@/lib/mcpPreferences";

export function useMcpRegistryCatalog(enabled = true) {
  const [services, setServices] = useState<RegistryService[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async () => {
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
      setServices(entries);
      setError(null);
      return entries;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load MCP services");
      return undefined;
    } finally {
      setLoading(false);
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
