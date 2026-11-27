// What the Markdown being drawn says of its message: whether it's still streaming in, including
// the throttled tail that trails the stream's end, and whether its renderer reads PR references.

import { createContext, useContext } from "react";

export const StreamingTextContext = createContext(false);
export const MarksPullRequestReferencesContext = createContext(false);

/** Whether the enclosing message's text is still arriving; false outside one. */
export function useMessageTextStreaming(): boolean {
  return useContext(StreamingTextContext);
}

/** Whether the enclosing message's renderer shows its PR references; false outside one. */
export function useMessageMarksPullRequestReferences(): boolean {
  return useContext(MarksPullRequestReferencesContext);
}
