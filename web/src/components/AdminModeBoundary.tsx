import { type ReactNode, useEffect, useSyncExternalStore } from "react";
import {
  hasAdminModeContextChanged,
  subscribeAdminModeChanges,
  usesBrowserAdminMode,
} from "@/lib/adminMode";
import {
  getIdentityResolutionState,
  isLoginRedirectPending,
  resolveIdentity,
  subscribeIdentity,
} from "@/lib/identity";
import { PageLoading } from "./PageLoading";
import { Button } from "./ui/button";

/** Keep permission-sensitive providers unmounted until browser mode is established. */
export function AdminModeBoundary({ children }: { children: ReactNode }) {
  return usesBrowserAdminMode() ? <AdminModeBootstrap>{children}</AdminModeBootstrap> : children;
}

function AdminModeBootstrap({ children }: { children: ReactNode }) {
  const state = useSyncExternalStore(
    subscribeIdentity,
    getIdentityResolutionState,
    getIdentityResolutionState,
  );
  const contextChanged = hasAdminModeContextChanged();
  useEffect(() => {
    const stopWatching = subscribeAdminModeChanges();
    if (!hasAdminModeContextChanged()) void resolveIdentity();
    return stopWatching;
  }, []);

  if (isLoginRedirectPending()) return <PageLoading label="Opening sign-in…" />;
  if (contextChanged) return <PageLoading />;
  if (state === "ready") return children;
  if (state === "retrying") {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 bg-background text-foreground">
        <p role="alert">Could not finish loading Omnigent. Retrying…</p>
        <Button variant="outline" onClick={() => void resolveIdentity()}>
          Retry now
        </Button>
      </div>
    );
  }
  return <PageLoading />;
}
