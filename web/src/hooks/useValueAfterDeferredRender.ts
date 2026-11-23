// A value that changes in its own background render, after both the urgent commit that changed it
// and the deferred render that follows it. Lets a change whose DOM effect is costly, such as a
// large subtree going inert, land in a frame of its own instead of adding to either of those.

import { startTransition, useDeferredValue, useEffect, useState } from "react";

export function useValueAfterDeferredRender<T>(value: T): T {
  const deferred = useDeferredValue(value);
  const [shown, setShown] = useState(value);
  useEffect(() => {
    if (Object.is(shown, deferred)) return;
    // The frame after the deferred commit, as a non-urgent render of its own.
    const frame = requestAnimationFrame(() => startTransition(() => setShown(deferred)));
    return () => cancelAnimationFrame(frame);
  }, [deferred, shown]);
  return shown;
}
