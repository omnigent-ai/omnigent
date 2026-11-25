// The embedded browser's own controls (its URL bar, ‹ › and reload) tell whoever
// tracks the view's history that the user took over the page, so a step still
// waiting for the page to load stops waiting at once. A typed address is a
// numbered submission: once the shell accepts its load, the acceptance names that
// submission, for shells that never report where a page went. Only a view a tabs
// owner holds numbers its submissions; elsewhere a typed load goes ahead as before.

export type BrowserPageControlEvent =
  /** The user took over the page; `submission` when it was a typed address. */
  | { kind: "takeover"; submission?: number }
  /** The shell accepted the load of `submission`, for `url`. Not a gesture of its own. */
  | { kind: "accepted"; submission: number; url: string };

type Listener = (event: BrowserPageControlEvent) => void;

const listeners = new Map<string, Set<Listener>>();
let submissions = 0;
/** Each view's typed address still wanted; a workspace landing, a page control or a newer submission replaces it. */
const currentSubmissions = new Map<string, number>();

function emit(viewId: string, event: BrowserPageControlEvent): void {
  for (const listener of [...(listeners.get(viewId) ?? [])]) listener(event);
}

/** Hear about the user using `viewId`'s own page controls. */
export function onBrowserPageControl(viewId: string, listener: Listener): () => void {
  let forView = listeners.get(viewId);
  if (!forView) {
    forView = new Set();
    listeners.set(viewId, forView);
  }
  forView.add(listener);
  return () => {
    forView.delete(listener);
    if (forView.size === 0 && listeners.get(viewId) === forView) listeners.delete(viewId);
  };
}

/** The user edited the address, used the page's ‹ or ›, or reloaded it: a typed load not yet issued is dropped. */
export function noteBrowserPageControl(viewId: string): void {
  currentSubmissions.delete(viewId);
  emit(viewId, { kind: "takeover" });
}

/**
 * The user submitted a typed address: a takeover whose load `acceptBrowserPageSubmission` may
 * confirm. Null where no owner holds `viewId`, so nothing but a newer submission replaces it.
 */
export function beginBrowserPageSubmission(viewId: string): number | null {
  if (!listeners.has(viewId)) return null;
  submissions += 1;
  currentSubmissions.set(viewId, submissions);
  emit(viewId, { kind: "takeover", submission: submissions });
  return submissions;
}

/** Whether `submission` is still the load the user wants in `viewId`: check before issuing it and before showing its result. */
export function isBrowserPageSubmissionCurrent(viewId: string, submission: number): boolean {
  return currentSubmissions.get(viewId) === submission;
}

/** Back, Forward or a link moved `viewId`'s page for the workspace: a typed load not yet issued is dropped. */
export function supersedeBrowserPageSubmission(viewId: string): void {
  currentSubmissions.delete(viewId);
}

/** The shell accepted `submission`'s load. Only the latest submission, with nothing since, counts. */
export function acceptBrowserPageSubmission(viewId: string, submission: number, url: string): void {
  emit(viewId, { kind: "accepted", submission, url });
}
