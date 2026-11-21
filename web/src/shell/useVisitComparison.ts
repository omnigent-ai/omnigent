// A workspace tab's visit to Changes keeps the comparison it showed. Landing on the visit again,
// by Back, Forward or a link, applies that comparison to the session, so the badge, the list
// and every review agree; a comparison the person picks is a new step.

import { useEffect, useRef } from "react";

import { type ComparisonChoice, sameComparisonChoice } from "@/lib/comparisonChoice";

/** Whether the session can show a comparison: now, once it knows more, or not here and why. */
export type ComparisonAvailability = "now" | "later" | { unavailable: string };

/**
 * A landing's comparison the session doesn't show yet: `waiting` until the session can tell
 * whether it can, or `unavailable` here, and why.
 */
export type PendingVisitComparison =
  { waiting: ComparisonChoice } | { unavailable: { choice: ComparisonChoice; reason: string } };

export interface VisitComparisonOptions {
  /** The review is on screen; only then does it apply or record a comparison. */
  active: boolean;
  /** The comparison the visit showed, if it recorded one. */
  visit: ComparisonChoice | undefined;
  /**
   * How the tab last landed on the visit, and the session's choice count then. Its comparison is
   * applied once per landing, unless the person picked one since; a tab shown again without a
   * landing, such as by a click or after a reload, keeps what the session shows.
   */
  landing: { settled: boolean; choices: number } | undefined;
  /** What the session shows, and how to apply a comparison to it. */
  session: {
    shown: ComparisonChoice;
    settled: boolean;
    availability: (choice: ComparisonChoice) => ComparisonAvailability;
    choose: (choice: ComparisonChoice) => void;
  };
  /** Changes each time the person chooses a comparison for the session. */
  choices: number;
  /**
   * The session shows a comparison the visit didn't record: `step` when the person picked
   * it here, else the visit takes it in place, as when it first shows or another window chose.
   */
  onShown: (shown: ComparisonChoice, options: { step: boolean }) => void;
}

function choiceKey(choice: ComparisonChoice | undefined): string {
  return JSON.stringify(choice ?? null);
}

export function useVisitComparison({
  active,
  visit,
  landing,
  session,
  choices,
  onShown,
}: VisitComparisonOptions): PendingVisitComparison | null {
  const visitKey = choiceKey(visit);
  const shownKey = choiceKey(session.shown);
  const availability = visit === undefined ? "now" : session.availability(visit);
  const availabilityKey = typeof availability === "string" ? availability : "unavailable";
  // The choice count when this review last asked the session for a visit's comparison.
  const applying = useRef<number | null>(null);
  // The choice count last recorded, so a pick here can be told from one elsewhere.
  const seenChoices = useRef(choices);
  const latest = useRef({ visit, session, onShown });
  latest.current = { visit, session, onShown };

  // A landing brings back the visit's comparison once the session can show it. A comparison the
  // person picked since it landed, here, in another tab or before this one rendered, stands.
  useEffect(() => {
    if (!landing || landing.settled) return;
    if (choices !== landing.choices) {
      landing.settled = true;
      return;
    }
    if (!active || !session.settled) return;
    const { visit: wanted, session: current } = latest.current;
    if (wanted === undefined || sameComparisonChoice(wanted, current.shown)) {
      landing.settled = true;
      return;
    }
    // A comparison that can't show yet waits, and one that can't show here stays asked for.
    if (availability !== "now") return;
    landing.settled = true;
    applying.current = choices;
    current.choose(wanted);
  }, [active, session.settled, landing, visitKey, availabilityKey, choices]); // eslint-disable-line react-hooks/exhaustive-deps

  // What the session shows otherwise is recorded on the visit.
  useEffect(() => {
    // A pick off screen is taken in place; one before the session is known is a step once it is.
    if (active && !session.settled) return;
    const picked = seenChoices.current !== choices;
    seenChoices.current = choices;
    if (!active) return;
    // A landing still waiting to show its comparison keeps the visit as it is.
    if (landing && !landing.settled) return;
    const { visit: recorded, session: current } = latest.current;
    if (sameComparisonChoice(recorded, current.shown)) {
      applying.current = null;
      return;
    }
    // The applied comparison is still on its way; its own choice doesn't count as a pick.
    if (applying.current !== null && choices <= applying.current + 1) return;
    applying.current = null;
    latest.current.onShown(current.shown, { step: picked && recorded !== undefined });
  }, [active, session.settled, landing, visitKey, shownKey, choices, availabilityKey]);

  // A landing nobody has picked past waits until its comparison shows; readiness only gates
  // applying it. Effects settle a landing without rendering, so one already shown isn't pending.
  const pending =
    active &&
    landing !== undefined &&
    !landing.settled &&
    landing.choices === choices &&
    visit !== undefined &&
    !sameComparisonChoice(visit, session.shown);
  if (!pending) return null;
  return session.settled && typeof availability !== "string"
    ? { unavailable: { choice: visit, reason: availability.unavailable } }
    : { waiting: visit };
}
