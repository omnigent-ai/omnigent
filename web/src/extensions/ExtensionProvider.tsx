import { createContext, useContext, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { EXTENSIONS_QUERY_KEY, fetchExtensionCatalog } from "./catalog";
import type { ExtensionCatalogItem } from "./types";

const EMPTY_EXTENSIONS: ExtensionCatalogItem[] = [];
const ExtensionContext = createContext<ExtensionCatalogItem[]>(EMPTY_EXTENSIONS);

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
  return <ExtensionContext.Provider value={extensions}>{children}</ExtensionContext.Provider>;
}

export function ExtensionProvider({ children }: { children: ReactNode }) {
  const query = useQuery({
    queryKey: EXTENSIONS_QUERY_KEY,
    queryFn: ({ signal }) => loadExtensionCatalog(signal),
    retry: false,
    staleTime: Infinity,
  });

  return (
    <ExtensionContext.Provider value={query.data ?? EMPTY_EXTENSIONS}>
      {children}
    </ExtensionContext.Provider>
  );
}

export function useExtensions(): ExtensionCatalogItem[] {
  return useContext(ExtensionContext);
}
