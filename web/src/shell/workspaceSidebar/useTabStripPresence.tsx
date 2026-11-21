// Runs the strip's presence model over the host's keyed tabs: the clock, the timer that
// ends each phase, and Reduce motion, which ends every phase the moment it turns on.

import {
  createContext,
  type ReactElement,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";

import { TAB_STRIP_MOTION } from "./tabStripMotion";
import {
  initialPresence,
  nextPresenceDeadline,
  type PresencePhase,
  type PresenceState,
  settleAllPresence,
  settlePresence,
  updatePresence,
} from "./tabStripPresence";

export interface StripEntryMotion {
  phase: PresencePhase;
}

const PRESENT: StripEntryMotion = { phase: "present" };

/** What a strip tab is doing; a tab outside a strip is simply present. */
export const StripEntryMotionContext = createContext<StripEntryMotion>(PRESENT);

export function useStripEntryMotion(): StripEntryMotion {
  return useContext(StripEntryMotionContext);
}

export interface StripEntry {
  key: string;
  tab: ReactElement;
}

export interface PresentStripEntry extends StripEntry {
  motion: StripEntryMotion;
}

const now = () => performance.now();

interface Tracked {
  scope: string;
  signature: string;
  state: PresenceState<ReactElement>;
}

/** `entries` are the strip's keyed tabs, in order; a new `scope` shows its tabs as they are. */
export function useTabStripPresence(
  scope: string,
  entries: readonly StripEntry[],
): PresentStripEntry[] {
  const keys = entries.map((entry) => entry.key);
  const signature = keys.join("\u0000");
  const reduced = usePrefersReducedMotion();

  // The tabs last put on screen, so a closed one's ghost can keep showing it.
  const committed = useRef(new Map<string, ReactElement>());
  const [tracked, setTracked] = useState<Tracked>(() => ({
    scope,
    signature,
    state: initialPresence<ReactElement>(keys),
  }));

  let current = tracked;
  if (current.scope !== scope) {
    // Another session's tabs: nothing came or went, so nothing moves.
    current = { scope, signature, state: initialPresence<ReactElement>(keys) };
  } else if (current.signature !== signature) {
    current = {
      scope,
      signature,
      state: reduced
        ? initialPresence<ReactElement>(keys)
        : updatePresence(current.state, keys, now(), (key) => committed.current.get(key)),
    };
  }
  if (reduced) {
    const settled = settleAllPresence(current.state);
    if (settled !== current.state) current = { ...current, state: settled };
  }
  if (current !== tracked) setTracked(current);

  useLayoutEffect(() => {
    committed.current = new Map(entries.map((entry) => [entry.key, entry.tab]));
  });

  useEffect(() => {
    const deadline = nextPresenceDeadline(tracked.state, TAB_STRIP_MOTION);
    if (deadline === null) return;
    const timer = setTimeout(
      () => {
        setTracked((latest) => {
          // A timer can fire a little early; the phase it was set for has still ended.
          const state = settlePresence(latest.state, Math.max(now(), deadline), TAB_STRIP_MOTION);
          return state === latest.state ? latest : { ...latest, state };
        });
      },
      Math.max(0, deadline - now()),
    );
    return () => clearTimeout(timer);
  }, [tracked.state]);

  const byKey = new Map(entries.map((entry) => [entry.key, entry.tab]));
  return current.state.slots.flatMap((slot) => {
    const tab = slot.phase === "leaving" ? slot.payload : byKey.get(slot.key);
    if (tab === undefined) return [];
    return [{ key: slot.key, motion: { phase: slot.phase }, tab }];
  });
}
