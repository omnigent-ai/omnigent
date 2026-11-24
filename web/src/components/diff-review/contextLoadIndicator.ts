// Feedback on a file's "N unmodified lines" separators while its full text loads
// for an expansion: busy while the request runs, and a note if it failed or didn't match. The
// separators live in the diff library's shadow DOM, so the state is marked on each
// file's host element and styled from inside; the header holds the operable Retry.

/** `mismatched`: the full text loaded isn't the text the patch was made from. */
export type ContextLoadState = "loading" | "failed" | "mismatched";

const LOADING_ATTRIBUTE = "data-context-loading";
const FAILED_ATTRIBUTE = "data-context-failed";
const MISMATCHED_ATTRIBUTE = "data-context-mismatched";

/** Shadow CSS for the separators of a file whose unchanged lines are loading or failed. */
export const CONTEXT_LOAD_CSS = `
  :host([${LOADING_ATTRIBUTE}]) [data-separator] [data-expand-button] {
    cursor: progress;
    opacity: 0.5;
  }
  :host([${LOADING_ATTRIBUTE}]) [data-separator-content]::before {
    content: "";
    display: inline-block;
    width: 0.75em;
    height: 0.75em;
    margin-inline-end: 0.5em;
    border: 1.5px solid currentColor;
    border-inline-end-color: transparent;
    border-radius: 50%;
    vertical-align: -0.1em;
    animation: context-load-spin 0.8s linear infinite;
  }
  :host([${LOADING_ATTRIBUTE}]) [data-unmodified-lines]::after {
    content: " · Loading lines…";
  }
  :host([${FAILED_ATTRIBUTE}]) [data-unmodified-lines]::after {
    content: " · Couldn’t load";
    color: var(--destructive);
  }
  :host([${MISMATCHED_ATTRIBUTE}]) [data-unmodified-lines]::after {
    content: " · Couldn’t load the full file";
  }
  @keyframes context-load-spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) {
    :host([${LOADING_ATTRIBUTE}]) [data-separator-content]::before { animation: none; }
  }
`;

const guardedHosts = new WeakSet<Element>();
const EXPAND_TARGETS = ["data-expand-button", "data-unmodified-lines"];

/** While a file's lines load, its expand controls add nothing to the expansion asked for. */
function ignoreExpandWhileLoading(event: Event): void {
  const host = event.currentTarget as Element;
  if (!host.hasAttribute(LOADING_ATTRIBUTE)) return;
  // The library expands from its arrows and from the "N unmodified lines" label alike.
  const onExpand = event
    .composedPath()
    .some(
      (node) =>
        node instanceof Element && EXPAND_TARGETS.some((attribute) => node.hasAttribute(attribute)),
    );
  if (!onExpand) return;
  event.stopPropagation();
  event.preventDefault();
}

/**
 * Mark each rendered file with its load state and guard its expand controls.
 * Files are rendered and recycled by the library, so call this after every render.
 */
export function markContextLoads(
  items: readonly { id: string; element: Element }[],
  stateFor: (itemId: string) => ContextLoadState | undefined,
): void {
  for (const { id, element } of items) {
    const state = stateFor(id);
    element.toggleAttribute(LOADING_ATTRIBUTE, state === "loading");
    element.toggleAttribute(FAILED_ATTRIBUTE, state === "failed");
    element.toggleAttribute(MISMATCHED_ATTRIBUTE, state === "mismatched");
    if (!guardedHosts.has(element)) {
      guardedHosts.add(element);
      // Capturing on the host runs before the library's own listener inside its shadow root.
      element.addEventListener("click", ignoreExpandWhileLoading, { capture: true });
    }
  }
}
