// When a session's host comes back: this app saw it go offline and then online,
// or a resume from this app brought it up. Views use it to read again at once and
// to say the machine is starting, rather than blaming what it answers meanwhile.
import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import { pullRequestInfoQueryKey } from "@/hooks/usePullRequests";
import { clearSessionHostTransportRecovery } from "@/lib/hostTransportRecovery";
import { useSandboxResumeStore } from "@/store/sandboxResume";

/** How long after a return the session's machine may still be starting. */
export const HOST_RETURN_WINDOW_MS = 60_000;
/** Both signals report one resume within this long of each other. */
export const HOST_RETURN_COALESCE_MS = 10_000;

export interface HostReturn {
  /** Distinct per return, so views can tell a second one from the first. */
  id: number;
  at: number;
  /** Which signals reported it; a second report from the same signal is a new return. */
  sawOnline: boolean;
  sawResume: boolean;
  /** Offline readings seen before it; one seen since means a later report is a new return. */
  offlineCycle: number;
}

interface HostReturnStore {
  bySession: Record<string, HostReturn>;
}

export const useHostReturnStore = create<HostReturnStore>(() => ({ bySession: {} }));

let lastId = 0;
// What the observer last saw, kept across remounts so they aren't read as changes.
const lastOnline = new Map<string, boolean | null>();
const lastResume = new Map<string, number>();
// How many times the observer saw each host go offline.
const offlineCycles = new Map<string, number>();

/** Record one report of the session's host coming back; returns whether it's a new return. */
function report(sessionId: string, signal: "online" | "resume", now: number): boolean {
  const latest = useHostReturnStore.getState().bySession[sessionId];
  const seen = signal === "online" ? latest?.sawOnline : latest?.sawResume;
  const offlineCycle = offlineCycles.get(sessionId) ?? 0;
  // The other signal reporting the same return: no offline reading came between them.
  const sameReturn =
    latest !== undefined &&
    !seen &&
    latest.offlineCycle === offlineCycle &&
    now - latest.at < HOST_RETURN_COALESCE_MS;
  const next: HostReturn = sameReturn
    ? { ...latest, [signal === "online" ? "sawOnline" : "sawResume"]: true }
    : {
        id: ++lastId,
        at: now,
        sawOnline: signal === "online",
        sawResume: signal === "resume",
        offlineCycle,
      };
  useHostReturnStore.setState((state) => ({
    bySession: { ...state.bySession, [sessionId]: next },
  }));
  return !sameReturn;
}

/**
 * Report each session's host liveness. Only an observed offline → online is a
 * return; a first reading, even online, is not.
 */
export function observeHostOnline(
  hostOnline: ReadonlyMap<string, boolean | null>,
  now = Date.now(),
): string[] {
  const returned: string[] = [];
  for (const [sessionId, online] of hostOnline) {
    const previous = lastOnline.get(sessionId);
    lastOnline.set(sessionId, online);
    if (online === false && previous !== false) {
      offlineCycles.set(sessionId, (offlineCycles.get(sessionId) ?? 0) + 1);
    }
    if (previous === false && online === true && report(sessionId, "online", now)) {
      returned.push(sessionId);
    }
  }
  return returned;
}

/** Report when this app's resumes saw each host come online. */
export function observeSandboxResumes(
  resumedAt: Readonly<Record<string, number>>,
  now = Date.now(),
): string[] {
  const returned: string[] = [];
  for (const [sessionId, at] of Object.entries(resumedAt)) {
    if (lastResume.get(sessionId) === at) continue;
    lastResume.set(sessionId, at);
    if (report(sessionId, "resume", now)) returned.push(sessionId);
  }
  return returned;
}

/** Whether `hostReturn` happened recently enough that the machine may still be starting. */
export function isHostReturnRecent(hostReturn: HostReturn | undefined, now: number): boolean {
  return hostReturn !== undefined && now - hostReturn.at < HOST_RETURN_WINDOW_MS;
}

/** The session's latest host return, or undefined. */
export function useHostReturn(sessionId: string | null | undefined): HostReturn | undefined {
  return useHostReturnStore((state) => (sessionId ? state.bySession[sessionId] : undefined));
}

/** Whether the session's host came back within the window; turns false when it closes. */
export function useRecentHostReturn(sessionId: string | null | undefined): boolean {
  const hostReturn = useHostReturn(sessionId);
  const [closedReturn, setClosedReturn] = useState<number>();
  useEffect(() => {
    if (!hostReturn) return;
    const remaining = hostReturn.at + HOST_RETURN_WINDOW_MS - Date.now();
    const close = () => setClosedReturn(hostReturn.id);
    if (remaining <= 0) {
      close();
      return;
    }
    const timer = setTimeout(close, remaining);
    return () => clearTimeout(timer);
  }, [hostReturn]);
  return (
    hostReturn !== undefined &&
    closedReturn !== hostReturn.id &&
    isHostReturnRecent(hostReturn, Date.now())
  );
}

/**
 * Watch for hosts coming back and read their pull requests again once per
 * return. Mounted once, where host liveness is merged.
 */
export function useHostReturnObserver(hostOnline: ReadonlyMap<string, boolean | null>): void {
  const queryClient = useQueryClient();
  const resumedAt = useSandboxResumeStore((state) => state.resumedAt);
  useEffect(() => {
    const returned = [...observeHostOnline(hostOnline), ...observeSandboxResumes(resumedAt)];
    for (const sessionId of returned) {
      clearSessionHostTransportRecovery(sessionId);
      void queryClient.invalidateQueries({ queryKey: ["pr-page-github-info", sessionId] });
      // The classic page keeps its own family; it reads again too.
      void queryClient.invalidateQueries({ queryKey: pullRequestInfoQueryKey(sessionId) });
    }
  }, [hostOnline, resumedAt, queryClient]);
}

/** Test seam: forget every return and observation. */
export function resetHostReturns(): void {
  useHostReturnStore.setState({ bySession: {} });
  lastOnline.clear();
  lastResume.clear();
  offlineCycles.clear();
}
