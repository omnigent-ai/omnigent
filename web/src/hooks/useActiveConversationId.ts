import { useLocation, useRebasePath } from "@/lib/routing";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { CANVAS_SESSION_PARAM } from "@/canvas/canvasNavigation";

/** Resolve the visible session above Routes, including embedded mount paths. */
export function useSessionRoute(): { isCanvas: boolean; conversationId: string | undefined } {
  const { pathname, search } = useLocation();
  const rebasePath = useRebasePath();
  const info = useServerInfo();
  const canvasPath = rebasePath("/canvas").toLowerCase();
  const routePath = pathname.toLowerCase();
  const isCanvas =
    isFeatureEnabled(info, "canvas") &&
    (routePath === canvasPath || routePath === `${canvasPath}/`);
  if (isCanvas) {
    return {
      isCanvas,
      conversationId: new URLSearchParams(search).get(CANVAS_SESSION_PARAM) || undefined,
    };
  }
  const prefix = rebasePath("/c/");
  if (!routePath.startsWith(prefix.toLowerCase()))
    return { isCanvas: false, conversationId: undefined };
  const segment = pathname.slice(prefix.length).replace(/\/$/, "");
  if (!segment || segment.includes("/")) return { isCanvas: false, conversationId: undefined };
  let conversationId = segment;
  try {
    conversationId = decodeURIComponent(segment);
  } catch {
    // Match the router's undecoded fallback for malformed URL encoding.
  }
  return { isCanvas: false, conversationId };
}

export function useActiveConversationId(): string | undefined {
  return useSessionRoute().conversationId;
}
