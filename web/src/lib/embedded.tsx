// Embed-mode detection seam.
//
// `false` standalone (`main.tsx` never mounts the provider), `true` when a host
// renders web through `embed.tsx` (which wraps the tree in
// `<EmbeddedProvider>`). Use it to branch UI that only makes sense in one mode
// — e.g. hide the theme switcher in the embed, where the host owns the theme
// and `embed.tsx` forces light via `NextThemesProvider forcedTheme="light"`.
//
// It's a context (not a module-level flag) so it's reactive, overridable in
// tests via the provider, and reads cleanly with a hook from any component.

import { createContext, type ReactNode, useContext } from "react";

const EmbeddedContext = createContext(false);

export function EmbeddedProvider({ children }: { children: ReactNode }) {
  return <EmbeddedContext.Provider value={true}>{children}</EmbeddedContext.Provider>;
}

/** True when web is rendered inside a host via `embed.tsx`. */
export function useIsEmbedded(): boolean {
  return useContext(EmbeddedContext);
}
