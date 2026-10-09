import { authenticatedFetch } from "@/lib/identity";
import type {
  ExtensionCatalogItem,
  ExtensionCatalogResponse,
  ResolvedExtensionPage,
} from "./types";

export const EXTENSIONS_QUERY_KEY = ["extensions"] as const;

function isCatalogResponse(value: unknown): value is ExtensionCatalogResponse {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ExtensionCatalogResponse>;
  return candidate.object === "list" && Array.isArray(candidate.data);
}

export async function fetchExtensionCatalog(signal?: AbortSignal): Promise<ExtensionCatalogItem[]> {
  const response = await authenticatedFetch("/v1/extensions", { signal });
  if (!response.ok) throw new Error(`Failed to load extensions (${response.status})`);
  const payload: unknown = await response.json();
  if (!isCatalogResponse(payload)) throw new Error("Invalid extension catalog response");
  return payload.data.filter((extension) => extension.status === "enabled");
}

export function extensionPathParts(
  pathname: string,
): { extensionId: string; route: string } | null {
  const marker = "/extensions/";
  const markerIndex = pathname.lastIndexOf(marker);
  if (markerIndex < 0) return null;
  const parts = pathname
    .slice(markerIndex + marker.length)
    .split("/")
    .filter(Boolean);
  if (parts.length !== 2 || !parts[0].includes(".")) return null;
  return { extensionId: parts[0], route: parts[1] };
}

export function resolveExtensionPageFromPath(
  extensions: ExtensionCatalogItem[],
  pathname: string,
): ResolvedExtensionPage | null {
  const parts = extensionPathParts(pathname);
  return parts ? resolveExtensionPage(extensions, parts.extensionId, parts.route) : null;
}

export function resolveExtensionPage(
  extensions: ExtensionCatalogItem[],
  extensionId: string | undefined,
  route: string | undefined,
): ResolvedExtensionPage | null {
  if (!extensionId || !route) return null;
  const extension = extensions.find((item) => item.id === extensionId);
  if (!extension) return null;
  const page = extension.pages.find((item) => item.route === route);
  return page ? { extension, page } : null;
}
