// A change sent again later, by Retry or a batch's next step, goes only as it was first sent:
// for the same workspace user, under the same sign-in, along the same Mac route.

import {
  currentWorkspaceAuthGeneration,
  draftOwner,
  macRouteCurrent,
  type MacWriteRoute,
} from "@/lib/desktopPullRequestWrites";

/** Who a change was first sent for and as. */
export interface MacSendScope {
  owner: string;
  authGeneration: number;
  route: MacWriteRoute;
}

/** The workspace user `owner`, as signed in now. */
export function macSendUser(owner: string): Pick<MacSendScope, "owner" | "authGeneration"> {
  return { owner, authGeneration: currentWorkspaceAuthGeneration() };
}

/** The scope a change `owner` sends now along `route` goes in. */
export function macSendScope(owner: string, route: MacWriteRoute): MacSendScope {
  return { ...macSendUser(owner), route };
}

/** Whether the same workspace user is still signed in, the same time, as `scope` was. */
export function macSendUserCurrent(scope: Pick<MacSendScope, "owner" | "authGeneration">) {
  return scope.owner === draftOwner() && scope.authGeneration === currentWorkspaceAuthGeneration();
}

/** The route `prUrl` would be sent along now, if all of `scope` still holds; null otherwise. */
export function macSendScopeCurrent(scope: MacSendScope, prUrl: string): MacWriteRoute | null {
  return macSendUserCurrent(scope) ? macRouteCurrent(prUrl, scope.route) : null;
}

/**
 * A Retry that sends again only within `scope`, checked at the click. For another workspace
 * user or sign-in it does nothing; where only the Mac route moved, `routeChanged` runs instead.
 */
export function guardedMacRetry(
  scope: MacSendScope,
  prUrl: string,
  { resend, routeChanged }: { resend: () => void; routeChanged?: () => void },
): () => void {
  return () => {
    if (!macSendUserCurrent(scope)) return;
    if (macRouteCurrent(prUrl, scope.route)) resend();
    else routeChanged?.();
  };
}
