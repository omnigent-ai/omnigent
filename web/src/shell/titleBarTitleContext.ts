import { createContext, useContext } from "react";

/**
 * Lets the authed {@link AppShell} publish the open thread's title up to the
 * macOS title-bar server picker, which is mounted at the top level (App.tsx) —
 * ABOVE the router — so it survives the unauthenticated `/login`, `/register`,
 * and setup pages where `AppShell` (and the picker's old mount) isn't rendered.
 *
 * Only `AppShell` has a conversation in scope, so it pushes the current
 * `<title> — <host>` label up here; on the unauthenticated pages the title is
 * left undefined and the picker shows just the brand. The default is a no-op so
 * non-Electron browsers, tests rendering `AppShell` in isolation, and any
 * consumer outside the provider can call it harmlessly.
 */
export type ThreadTitleSetter = (title: string | null | undefined) => void;

export const TitleBarTitleContext = createContext<ThreadTitleSetter>(() => {});

/** Setter for the title-bar picker's thread label. No-op outside the provider. */
export function useSetTitleBarTitle(): ThreadTitleSetter {
  return useContext(TitleBarTitleContext);
}
