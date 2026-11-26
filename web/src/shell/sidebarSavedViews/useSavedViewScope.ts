// Whose sidebar views show: the server this page talks to and its signed-in user.

import { useMemo } from "react";

import { useViewerId } from "@/hooks/useViewerId";
import { getBasePath } from "@/lib/basePath";
import type { SavedViewScope } from "@/lib/sidebarSavedViews";

/** The views' scope, or null until the viewer is known. */
export function useSavedViewScope(): SavedViewScope | null {
  const userId = useViewerId();
  return useMemo(
    () =>
      userId === null || typeof window === "undefined"
        ? null
        : { server: `${window.location.origin}${getBasePath()}`, userId },
    [userId],
  );
}
