// Gadget code opens pages by URL. Inside the app they open in place, through the app's router,
// so the shell (and a live session) stays mounted.
import { useCallback } from "react";

import { withBasePath } from "@/lib/basePath";
import { getOmnigentTransformShareLink } from "@/lib/host";
import { useNavigate } from "@/lib/routing";
import { gadgetBoardPath } from "@/gadgets/host/gadgetPages";
import type { GadgetNavigate } from "@/gadgets/host/gadgetSession";

/** Link state saying whether opening a gadget full screen leaves the sidebar as it is. */
export interface GadgetOpenState {
  keepSidebar?: boolean;
}

/**
 * The state for every link or navigation inside Omnigent that opens a gadget's page: the sidebar
 * stays as it is. A host's router can give in-app entries the key of a fresh load, so the key
 * alone can't tell them apart.
 */
export const IN_APP_GADGET_OPEN: GadgetOpenState = { keepSidebar: true };

/**
 * Whether a gadget opened full screen leaves the sidebar as it is: yes when the viewer opened it
 * from inside Omnigent, no for a link opened in a new tab or pasted into the address bar, which
 * opens it with the sidebar closed. Link state that says so wins.
 */
export function gadgetOpenKeepsSidebar(location: { key: string; state: unknown }): boolean {
  const kept = (location.state as GadgetOpenState | null)?.keepSidebar;
  if (typeof kept === "boolean") return kept;
  // The router names the entry the browser loaded "default"; each navigation in the app gets
  // its own key, which the browser keeps across a reload.
  return location.key !== "default";
}

/** The app path of a same-origin URL, or the path itself. */
export function appPathFromUrl(url: string): string {
  if (url.startsWith("/")) return url;
  const parsed = new URL(url, window.location.origin);
  return `${parsed.pathname}${parsed.search}${parsed.hash}`;
}

const GADGET_PAGE_PATH = /^\/gadget\/[^/?#]+\/?(?:[?#]|$)/;

/** Opens a URL from gadget code in this tab, replacing the current entry with `replace`. */
export function useGadgetNavigate({ replace = false }: { replace?: boolean } = {}): GadgetNavigate {
  const navigate = useNavigate();
  return useCallback(
    (url: string) => {
      const path = appPathFromUrl(url);
      navigate(path, {
        replace,
        ...(GADGET_PAGE_PATH.test(path) ? { state: IN_APP_GADGET_OPEN } : {}),
      });
    },
    [navigate, replace],
  );
}

/** What went wrong, in the server's words when it said. */
export function gadgetErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return typeof error === "string" && error ? error : "Something went wrong.";
}

/**
 * The absolute URL that opens a gadget, like a session's share link: through the host's link
 * transform in the embed, else this page's origin and base path.
 */
export function gadgetLink(gadgetId: string, rebasePath: (path: string) => string): string {
  const path = rebasePath(gadgetBoardPath(gadgetId));
  const transform = getOmnigentTransformShareLink();
  return transform ? transform(path) : `${window.location.origin}${withBasePath(path)}`;
}
