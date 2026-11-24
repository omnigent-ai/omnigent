import { useEffect } from "react";
import { registerRouteLeaveGuard } from "@/lib/routeLeaveGuards";

export function confirmGoogleDocsNavigation(action: string): boolean {
  return window.confirm(
    `${action}? Check that Google Docs has saved your edits and comments before continuing.`,
  );
}

const editors = new Set<symbol>();
const confirmEditorLeave = () => confirmGoogleDocsNavigation("Leave the document editors");
const warnBeforeUnload = (event: BeforeUnloadEvent) => event.preventDefault();

export function useGoogleDocsEditorLifecycle(open: boolean, sessionId?: string): void {
  useEffect(() => {
    if (!open) return;
    const token = Symbol();
    if (editors.size === 0) {
      window.addEventListener("beforeunload", warnBeforeUnload);
    }
    const releaseRouteGuard = registerRouteLeaveGuard(confirmEditorLeave, sessionId);
    editors.add(token);
    return () => {
      editors.delete(token);
      releaseRouteGuard();
      if (editors.size === 0) {
        window.removeEventListener("beforeunload", warnBeforeUnload);
      }
    };
  }, [open, sessionId]);
}
