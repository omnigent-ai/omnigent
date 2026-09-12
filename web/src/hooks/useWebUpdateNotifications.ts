import { useEffect, useRef, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";
import { showNotification } from "@/lib/browserNotifications";
import { sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";
import { useLocation } from "@/lib/routing";

// Routine checks run every five minutes, with one short follow-up for confirmation.
const POLL_MS = 5 * 60 * 1000;
const CONFIRM_MS = 1_000;

/** Mount once per page: compare deploys to the first observed build, never auto-reload. */
export function useWebUpdateNotifications() {
  const location = useLocation();
  const currentPath = useRef("");
  currentPath.current = location.pathname + location.search + location.hash;
  const baseline = useRef<string | null>(null);
  const candidate = useRef<string | null>(null);
  const notified = useRef(new Set<string>());
  const pendingNativeBuild = useRef<string | null>(null);
  const [availableBuildId, setAvailableBuildId] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    let request: AbortController | null = null;
    let confirmation: number | undefined;
    let focused = document.hasFocus();

    const notifyIfUnfocused = () => {
      const id = pendingNativeBuild.current;
      if (!id || (focused && document.visibilityState !== "hidden")) return;
      pendingNativeBuild.current = null;
      showNotification({
        title: "Update available",
        body: "Reload Omnigent to use the latest web app.",
        tag: `omnigent:web-update:${id}`,
        // Android requires a path to attach its existing tap-to-foreground intent.
        navigatePath: currentPath.current,
      });
    };

    const poll = async (isConfirmation = false) => {
      if (disposed || request) return;
      window.clearTimeout(confirmation);
      confirmation = undefined;
      const controller = new AbortController();
      request = controller;
      const timeout = window.setTimeout(() => controller.abort(), 30_000);
      try {
        const response = await authenticatedFetch("/api/version", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Version unavailable");
        const data = await response.json();
        if (disposed || controller.signal.aborted) return;
        const id = data?.webapp_build_id;
        if (typeof id !== "string" || !id) {
          candidate.current = null;
          return;
        }
        if (baseline.current === null) baseline.current = id;
        if (id === baseline.current || notified.current.has(id)) {
          candidate.current = null;
          return;
        }
        // Confirm once promptly; mixed replicas must not create a rapid polling loop.
        if (candidate.current !== id) {
          candidate.current = id;
          if (!isConfirmation) {
            confirmation = window.setTimeout(() => void poll(true), CONFIRM_MS);
          }
          return;
        }
        notified.current.add(id);
        setAvailableBuildId(id);
        pendingNativeBuild.current = id;
        notifyIfUnfocused();
      } catch {
        if (!disposed) candidate.current = null;
      } finally {
        window.clearTimeout(timeout);
        request = null;
      }
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") void poll();
      else notifyIfUnfocused();
    };
    const onOnline = () => void poll();
    const onFocus = () => {
      focused = true;
    };
    const onBlur = () => {
      focused = false;
      notifyIfUnfocused();
    };
    const unsubscribe = sessionUpdatesSocket.subscribeStatus(() => {
      if (sessionUpdatesSocket.isConnected()) void poll();
    });
    const interval = window.setInterval(() => void poll(), POLL_MS);
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("online", onOnline);
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);
    window.addEventListener("pointerdown", onFocus);
    window.addEventListener("keydown", onFocus);
    void poll();
    return () => {
      disposed = true;
      request?.abort();
      window.clearTimeout(confirmation);
      window.clearInterval(interval);
      unsubscribe();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("pointerdown", onFocus);
      window.removeEventListener("keydown", onFocus);
    };
  }, []);

  return {
    availableBuildId,
    dismiss: () => {
      pendingNativeBuild.current = null;
      setAvailableBuildId(null);
    },
  };
}
