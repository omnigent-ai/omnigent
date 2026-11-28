// Which pull request view a control is in, and which view a list's rows open into, so a row's
// request to open a control reaches the view the row selects its PR in and no other copy.

import {
  createContext,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useSyncExternalStore,
} from "react";

import { draftOwner } from "@/lib/workspaceDraftOwner";
import { subscribeIdentity } from "@/lib/identity";

import {
  cancelControlRequests,
  cancelControlRequestsFor,
  type ControlPlace,
  dropControlRequestsNotFor,
  type PullRequestControl,
  takeControlRequest,
  type TakenControlRequest,
  useControlRequestSerial,
  waitingControlFor,
} from "./controlRequests";

export interface PullRequestDestination {
  id: string;
  /** Shown to the person now, not hidden or behind another view. */
  active: boolean;
}

/** The pull request view the tree below is in. */
export const PullRequestDestinationContext = createContext<PullRequestDestination | null>(null);

/** The view a list's rows open their pull requests in. */
export const PullRequestRowDestinationContext = createContext<string | null>(null);

/** Where a control is: its view. */
export function useControlPlace(): ControlPlace & { active: boolean } {
  const destination = useContext(PullRequestDestinationContext);
  // A control outside any view, e.g. in a test, is always shown and takes only unaimed requests.
  return { destination: destination?.id ?? null, active: destination?.active ?? true };
}

/** Where a list's rows aim their requests: the view they open into. */
export function useRowControlPlace(): ControlPlace {
  const destination = useContext(PullRequestRowDestinationContext);
  useDropRequestsOnIdentityChange(destination);
  // One object while it stands, so row actions built from it keep their identity.
  return useMemo(() => ({ destination }), [destination]);
}

/**
 * Drop what's aimed at `destination` once the workspace user it's seen with here changes,
 * whether or not anything is ready to take it; StrictMode's rerun changes nothing.
 */
function useDropRequestsOnIdentityChange(destination: string | null): void {
  const owner = useSyncExternalStore(subscribeIdentity, draftOwner, draftOwner);
  const seen = useRef(owner);
  useEffect(() => {
    const before = seen.current;
    seen.current = owner;
    if (destination === null || before === owner) return;
    dropControlRequestsNotFor(destination);
  }, [destination, owner]);
}

/**
 * A pull request view as a destination for requests: `explicit` names it for a list outside
 * it, else it's its own. What waits for it is dropped as it shows another PR, goes, or reads
 * as another workspace user; `waiting` is what waits for the PR it shows, while it's shown.
 */
export function useControlDestination(
  explicit: string | undefined,
  selected: string | undefined,
  active: boolean,
): { value: PullRequestDestination; waiting: PullRequestControl | null; serial: number } {
  const own = useId();
  const id = explicit ?? own;
  const serial = useControlRequestSerial();
  useEffect(() => {
    cancelControlRequests(id, selected);
    return () => cancelControlRequestsFor(id, selected);
  }, [id, selected]);
  useDropRequestsOnIdentityChange(id);
  const value = useMemo(() => ({ id, active }), [id, active]);
  return { value, waiting: active ? waitingControlFor(id, selected) : null, serial };
}

/**
 * Take a request to open `control` on `prUrl` once this view is shown and the control `ready`
 * (loaded and editable), and run `open` with it. A control that goes while still `shown`, as a
 * section does when its PR finishes loading, hands the request back for its replacement.
 */
export function useTakenControlRequest<Detail>(
  prUrl: string,
  control: PullRequestControl,
  ready: boolean,
  open: (request: TakenControlRequest<Detail>) => void,
  shown: boolean,
): void {
  const place = useControlPlace();
  const serial = useControlRequestSerial();
  const latest = useRef(open);
  latest.current = open;
  const held = useRef<TakenControlRequest<Detail> | null>(null);
  const { destination, active } = place;
  useDropRequestsOnIdentityChange(destination);
  useEffect(() => {
    if (!active || !ready) return;
    const request = takeControlRequest<Detail>(prUrl, control, { destination });
    if (!request) return;
    held.current = request;
    latest.current(request);
  }, [serial, active, ready, prUrl, control, destination]);
  // Closed by the person, the request is done with.
  const wasShown = useRef(shown);
  useEffect(() => {
    if (wasShown.current && !shown) held.current = null;
    wasShown.current = shown;
  }, [shown]);
  useEffect(
    () => () => {
      const request = held.current;
      // Taken but not yet closed, including one taken just before the control went.
      if (request) request.release(request.detail);
    },
    [],
  );
}
