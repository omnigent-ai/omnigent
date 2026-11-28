// A request from outside a pull request's view, such as a list row or ⌘K, to open one of its
// controls: the Review menu, or the Reviewers or Labels picker. It's aimed at one view, for the
// workspace user it was made under, and only that view's control takes it. It opens
// a control and nothing else: what the control then sends keeps its own checks.

import { useSyncExternalStore } from "react";

import { draftOwner } from "@/lib/workspaceDraftOwner";

export type PullRequestControl = "review" | "reviewers" | "labels";

/** How long a request waits for its control to show. */
export const CONTROL_REQUEST_WAIT_MS = 15_000;

interface ControlRequest<Detail> {
  prUrl: string;
  control: PullRequestControl;
  /** The view it's aimed at; null for one made where no view names itself. */
  destination: string | null;
  owner: string;
  /** What the control opens on, e.g. the Review menu's verdict. */
  detail: Detail;
  serial: number;
  at: number;
  taken: boolean;
}

/** Where a control is, as it asks for a request. */
export interface ControlPlace {
  destination: string | null;
}

export interface TakenControlRequest<Detail> {
  detail: Detail;
  /**
   * Hand it back, for a control that goes while it's up, opening on `detail` when taken again;
   * only its own view can take it.
   */
  release: (detail: Detail) => void;
}

let pending: ControlRequest<unknown> | null = null;
let serial = 0;
const listeners = new Set<() => void>();

function changed() {
  serial += 1;
  for (const listener of listeners) listener();
}

/** Ask `place`'s view to open `control` on `prUrl`; it replaces any request still waiting. */
export function requestPullRequestControl<Detail>(
  prUrl: string,
  control: PullRequestControl,
  place: ControlPlace,
  detail: Detail,
): void {
  pending = {
    prUrl,
    control,
    destination: place.destination,
    owner: draftOwner(),
    detail,
    serial: serial + 1,
    at: Date.now(),
    taken: false,
  };
  changed();
}

/**
 * The waiting request for `control` on `prUrl` in `place`, taking it so nothing else opens for
 * it; undefined when none waits there. One for the same view made under another workspace user
 * is dropped instead: it was never this one's.
 */
export function takeControlRequest<Detail>(
  prUrl: string,
  control: PullRequestControl,
  place: ControlPlace,
  now = Date.now(),
): TakenControlRequest<Detail> | undefined {
  const request = pending as ControlRequest<Detail> | null;
  if (!request || request.taken || request.destination !== place.destination) return undefined;
  if (request.prUrl !== prUrl || request.control !== control) return undefined;
  if (now - request.at > CONTROL_REQUEST_WAIT_MS) return undefined;
  if (request.owner !== draftOwner()) {
    pending = null;
    changed();
    return undefined;
  }
  request.taken = true;
  return {
    detail: request.detail,
    // The replacement shows right away, so the wait starts over from the hand-back.
    release: (detail) => {
      if (pending !== request) return;
      request.taken = false;
      request.at = Date.now();
      request.detail = detail;
    },
  };
}

/**
 * Drop what's waiting for `destination` as its view moves to another pull request; a request
 * for `keeping` stays, since that's where the view went.
 */
export function cancelControlRequests(
  destination: string,
  keeping: string | null | undefined,
): void {
  if (!pending || pending.destination !== destination || pending.taken) return;
  if (keeping && pending.prUrl === keeping) return;
  pending = null;
  changed();
}

/**
 * Drop the request for `prUrl` in `destination` as the view showing it goes, taken or not, so
 * a control going with it can't hand it back. One for another PR stays: a view that opens a
 * new PR in a fresh copy of itself takes that.
 */
export function cancelControlRequestsFor(
  destination: string,
  prUrl: string | null | undefined,
): void {
  if (!pending || pending.destination !== destination || pending.prUrl !== prUrl) return;
  pending = null;
  changed();
}

/**
 * Drop the request aimed at `destination` once it no longer matches the workspace user, taken or
 * not, so switching user away and back can't bring it back.
 */
export function dropControlRequestsNotFor(destination: string): void {
  if (!pending || pending.destination !== destination) return;
  if (pending.owner === draftOwner()) return;
  pending = null;
  changed();
}

/** The control a request waiting for `destination` asks for on `prUrl`, untaken; else null. */
export function waitingControlFor(
  destination: string,
  prUrl: string | null | undefined,
  now = Date.now(),
): PullRequestControl | null {
  const request = pending;
  if (!request || request.taken || request.destination !== destination) return null;
  if (request.prUrl !== prUrl || now - request.at > CONTROL_REQUEST_WAIT_MS) return null;
  return request.control;
}

/** Changes whenever a request is made, taken back or dropped, so a shown control checks it. */
export function useControlRequestSerial(): number {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => serial,
    () => 0,
  );
}

export function resetControlRequestsForTests(): void {
  pending = null;
  changed();
}
