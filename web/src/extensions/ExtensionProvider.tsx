import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { EXTENSIONS_QUERY_KEY, fetchExtensionCatalog } from "./catalog";
import type { ExtensionCatalogItem } from "./types";

const EMPTY_EXTENSIONS: ExtensionCatalogItem[] = [];
const NOOP_REFRESH = async () => EMPTY_EXTENSIONS;
interface ExtensionContextValue {
  extensions: ExtensionCatalogItem[];
  refresh: () => Promise<ExtensionCatalogItem[]>;
}
const ExtensionContext = createContext<ExtensionContextValue>({
  extensions: EMPTY_EXTENSIONS,
  refresh: NOOP_REFRESH,
});

export async function loadExtensionCatalog(
  signal?: AbortSignal,
  fetcher: typeof fetchExtensionCatalog = fetchExtensionCatalog,
): Promise<ExtensionCatalogItem[]> {
  try {
    return await fetcher(signal);
  } catch {
    console.warn("Extension catalog is unavailable");
    return EMPTY_EXTENSIONS;
  }
}

export function ExtensionCatalogProvider({
  extensions,
  children,
}: {
  extensions: ExtensionCatalogItem[];
  children: ReactNode;
}) {
  const value = useMemo<ExtensionContextValue>(
    () => ({ extensions, refresh: async () => extensions }),
    [extensions],
  );
  return <ExtensionContext.Provider value={value}>{children}</ExtensionContext.Provider>;
}

export function ExtensionProvider({ children }: { children: ReactNode }) {
  const query = useQuery({
    queryKey: EXTENSIONS_QUERY_KEY,
    queryFn: ({ signal }) => loadExtensionCatalog(signal),
    retry: false,
    staleTime: Infinity,
  });

  const extensions = query.data ?? EMPTY_EXTENSIONS;
  const { refetch } = query;
  const refresh = useCallback(async () => (await refetch()).data ?? EMPTY_EXTENSIONS, [refetch]);
  const value = useMemo(() => ({ extensions, refresh }), [extensions, refresh]);
  return <ExtensionContext.Provider value={value}>{children}</ExtensionContext.Provider>;
}

export function useExtensions(): ExtensionCatalogItem[] {
  return useContext(ExtensionContext).extensions;
}

export function useRefreshExtensions(): () => Promise<ExtensionCatalogItem[]> {
  return useContext(ExtensionContext).refresh;
}
