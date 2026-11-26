// The Markdown files a review renders as a rich diff: the reader's stored choice, shown a little
// after it's made, so the click itself stays short. The choice stays the reader's intent; this
// is only what's on screen, and it always belongs to the review that's showing.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { deferPresentation } from "./deferPresentation";

interface Presented {
  scope: string;
  paths: ReadonlySet<string>;
}

/**
 * The paths `scope`'s review shows as rich, following `intended` after a deferral. Another
 * review shows its own choice at once: a pending change from the last review never reaches it.
 */
export function usePresentedRichPaths(
  scope: string,
  intended: ReadonlySet<string>,
): ReadonlySet<string> {
  const [presented, setPresented] = useState<Presented>(() => ({ scope, paths: intended }));
  // A new review starts from its own choice, in this render.
  const current = presented.scope === scope ? presented : { scope, paths: intended };
  if (current !== presented) setPresented(current);
  // The choice as of the last commit, which a deferred update reads when it runs.
  const latest = useRef({ scope, intended });
  useLayoutEffect(() => {
    latest.current = { scope, intended };
  });
  // Every new choice queues an update, even one matching what shows now: a reader withdrawing
  // a choice whose render React still holds must supersede it, or that render still commits.
  const queued = useRef({ scope, intended });
  useEffect(() => {
    if (queued.current.scope === scope && queued.current.intended === intended) return;
    queued.current = { scope, intended };
    deferPresentation(() => {
      const now = latest.current;
      // The review changed meanwhile: its own choice is already showing.
      if (now.scope !== scope) return;
      // The newest choice, whichever change scheduled this; never over another review's.
      setPresented((previous) => {
        if (previous.scope !== now.scope || previous.paths === now.intended) return previous;
        return { scope: now.scope, paths: now.intended };
      });
    });
  }, [scope, intended]);
  return current.paths;
}
