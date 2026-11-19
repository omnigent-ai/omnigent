// Thingy code opens pages by URL. Inside the app they open in place, through the app's router,
// so the shell (and a live session) stays mounted.
import { useCallback } from "react";

import { useNavigate } from "@/lib/routing";
import type { ThingyNavigate } from "@/thingies/host/thingySession";

/** The app path of a same-origin URL, or the path itself. */
export function appPathFromUrl(url: string): string {
  if (url.startsWith("/")) return url;
  const parsed = new URL(url, window.location.origin);
  return `${parsed.pathname}${parsed.search}${parsed.hash}`;
}

/** Opens a URL from thingy code in this tab, replacing the current entry with `replace`. */
export function useThingyNavigate({ replace = false }: { replace?: boolean } = {}): ThingyNavigate {
  const navigate = useNavigate();
  return useCallback(
    (url: string) => navigate(appPathFromUrl(url), { replace }),
    [navigate, replace],
  );
}

/** What went wrong, in the server's words when it said. */
export function thingyErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return typeof error === "string" && error ? error : "Something went wrong.";
}
