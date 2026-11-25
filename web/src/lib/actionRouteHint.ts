// The tooltip a pull request action adds on the new page when its change won't go where
// the PR is read: read on this Mac, while changes go through the session's machine.

import { createContext, useContext } from "react";

import { readsOnThisMac, useDesktopReadPathVersion } from "@/lib/desktopReadPath";
import { useMacReadAs } from "@/hooks/macReadAs";
import { macWriteDispatch, useMacWriteRouteVersion } from "@/lib/desktopPullRequestWrites";

/** On where a page asks its actions to say so; the classic panel leaves it off. */
export const ActionRouteHintsContext = createContext(false);

/** `verb` phrased for the route, or undefined where the change goes where the PR is read. */
export function actionRouteHint(
  verb: string,
  { readOnMac, writesOnMac }: { readOnMac: boolean; writesOnMac: boolean },
): string | undefined {
  return readOnMac && !writesOnMac ? `${verb} through the session’s machine` : undefined;
}

/** The hint for `verb` on `prUrl`, live as the read and write routes change. */
export function useActionRouteHint(
  prUrl: string | null | undefined,
  verb: string,
): string | undefined {
  const on = useContext(ActionRouteHintsContext);
  useDesktopReadPathVersion();
  useMacWriteRouteVersion();
  const readAs = useMacReadAs();
  if (!on || !prUrl) return undefined;
  return actionRouteHint(verb, {
    readOnMac: readsOnThisMac(prUrl),
    // A change blocked as the page's own login goes nowhere, not to the session's machine.
    writesOnMac: macWriteDispatch(prUrl, readAs).kind !== "host",
  });
}
