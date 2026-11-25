// Keeping one file in place on screen while it changes height, as when it
// switches between its source and rich Markdown diff.

import type { MarkdownPreviewState } from "./markdownDiffStore";

/**
 * Where a file's top should stay, measured from the top of the review's
 * viewport. A reader partway into the file, with its top scrolled away, keeps
 * the file at the top of the viewport, starting from its beginning.
 */
export function fileAnchorOffset(fileTop: number, viewportTop: number): number {
  return Math.max(fileTop - viewportTop, 0);
}

/** How far to scroll to put the file's top back at `offset`; 0 when it's within a pixel. */
export function anchorDrift(fileTop: number, viewportTop: number, offset: number): number {
  const drift = fileTop - viewportTop - offset;
  return Math.abs(drift) < 1 ? 0 : drift;
}

/** Whether a switched file is done changing height: a source diff at once, a rich one once its preview has an answer. */
export function fileViewSettled(rich: boolean, state: MarkdownPreviewState | undefined): boolean {
  return !rich || (state !== undefined && state.status !== "loading");
}

/**
 * Whether a file switched to `rich` is done changing height: the view the diff view rendered
 * last, `committedRich`, is the one asked for, and that view is settled. A switch that hasn't
 * rendered yet isn't done, even if a cached preview is ready.
 */
export function switchSettled(
  rich: boolean,
  committedRich: boolean,
  state: MarkdownPreviewState | undefined,
): boolean {
  return committedRich === rich && fileViewSettled(rich, state);
}

/** How holding a target in place ended. */
export type AlignOutcome = "aligned" | "exhausted" | "abandoned";

/** Where the target and the view are now, as viewport tops. */
export type AlignReading =
  | { targetTop: number; viewTop: number }
  /** Not rendered yet: the next render tries again. */
  | "pending"
  /** The placement went stale, its time ran out, or the view went away. */
  | "abandoned";

/**
 * One placement holding a target `offset` from the view's top while layout settles. The
 * returned attempt runs after each render and frame; it corrects with `scrollBy` at most
 * `corrections` times, keeps looking while `settled` says layout still changes, and calls `done`
 * exactly once, after which further attempts, such as frames already queued, do nothing.
 */
export function createAlignment(options: {
  offset: number;
  corrections: number;
  settled?: () => boolean;
  read: () => AlignReading;
  scrollBy: (top: number) => void;
  afterLayout: (next: () => void) => void;
  done: (outcome: AlignOutcome) => void;
}): () => void {
  let corrections = options.corrections;
  let finished = false;
  // One look after layout at a time, however many renders ask for one.
  let looking = false;
  const lookAfterLayout = () => {
    if (looking) return;
    looking = true;
    options.afterLayout(() => {
      looking = false;
      attempt();
    });
  };
  const stop = (outcome: AlignOutcome) => {
    finished = true;
    options.done(outcome);
  };
  function attempt() {
    if (finished) return;
    const reading = options.read();
    if (reading === "abandoned") return stop("abandoned");
    if (reading === "pending") return;
    const drift = anchorDrift(reading.targetTop, reading.viewTop, options.offset);
    // Held in place, but still changing: look again after the next layout.
    if (drift === 0 && options.settled && !options.settled()) return lookAfterLayout();
    if (drift === 0) return stop("aligned");
    if (corrections === 0) return stop("exhausted");
    corrections -= 1;
    options.scrollBy(drift);
    lookAfterLayout();
  }
  return attempt;
}
