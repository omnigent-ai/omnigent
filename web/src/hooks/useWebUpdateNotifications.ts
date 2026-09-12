import { useEffect, useRef, useState } from "react";
import { authenticatedFetch } from "@/lib/identity";
import { showNotification } from "@/lib/browserNotifications";
import { sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";
import { useLocation } from "@/lib/routing";

// ponytail: 5min polling caps scheduled checks at 12/hour; upgrade to a socket build signal.
const POLL_MS = 5 * 60 * 1000;

/** Mount once per page: compare deploys to the first observed build, never auto-reload. */
export function useWebUpdateNotifications() {
  const location = useLocation();
  const currentPath = useRef("");
  currentPath.current = location.pathname + location.search + location.hash;
  const baseline = useRef<string | null>(null);
  const candidate = useRef<string | null>(null);
  const notified = useRef(new Set<string>());
  const [availableBuildId, setAvailableBuildId] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    let request: AbortController | null = null;
    let focused = document.hasFocus();

    const poll = async () => {
      if (disposed || request) return;
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
        if (id === baseline.current) {
          candidate.current = null;
          return;
        }
        // ponytail: two matching reads limit replica flapping, with up to 10min detection;
        // upgrade to an authoritative deploy signal if replicas remain mixed longer.
        if (candidate.current !== id) {
          candidate.current = id;
          return;
        }
        if (notified.current.has(id)) return;
        notified.current.add(id);
        setAvailableBuildId(id);
        if (!focused || document.visibilityState === "hidden") {
          showNotification({
            title: "Update available",
            body: "Reload Omnigent to use the latest web app.",
            tag: `omnigent:web-update:${id}`,
            // Android requires a path to attach its existing tap-to-foreground intent.
            navigatePath: currentPath.current,
          });
        }
      } catch {
        if (!disposed) candidate.current = null;
      } finally {
        window.clearTimeout(timeout);
        request = null;
      }
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") void poll();
    };
    const onOnline = () => void poll();
    const onFocus = () => {
      focused = true;
    };
    const onBlur = () => {
      focused = false;
    };
    const unsubscribe = sessionUpdatesSocket.subscribeStatus(() => {
      if (sessionUpdatesSocket.isConnected()) void poll();
    });
    const interval = window.setInterval(onVisible, POLL_MS);
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onOnline);
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);
    window.addEventListener("pointerdown", onFocus);
    window.addEventListener("keydown", onFocus);
    void poll();
    return () => {
      disposed = true;
      request?.abort();
      window.clearInterval(interval);
      unsubscribe();
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("pointerdown", onFocus);
      window.removeEventListener("keydown", onFocus);
    };
  }, []);

  return { availableBuildId, dismiss: () => setAvailableBuildId(null) };
}
