// Which acceptance a picker dialog's close belongs to. Each accept owns what follows from it, a
// deferred completion and the close it asks for, until a cancel, a reopen or a newer accept
// takes that over; a completion or close action from an acceptance that no longer owns it is dropped.

/** One acceptance; compared by identity only. */
export type AcceptanceToken = { readonly kind: "acceptance" };

export interface AcceptanceOwnership {
  /** Starts an acceptance, superseding any earlier one, and returns its token. */
  begin: () => AcceptanceToken;
  isCurrent: (token: AcceptanceToken) => boolean;
  /** Ends the current acceptance, as a cancel, a reopen or an unmount does. */
  revoke: () => void;
  /** `token` asks the dialog to close, running `afterClose` once it has. */
  requestClose: (token: AcceptanceToken, afterClose: (() => void) | null) => void;
  /** Whether the current acceptance asked for a close that hasn't finished. */
  closing: () => boolean;
  /**
   * The action for the close now finishing, if that close's acceptance still owns it, else
   * null. Either way the close is spent.
   */
  takeAfterClose: () => (() => void) | null;
}

export function createAcceptanceOwnership(): AcceptanceOwnership {
  let current: AcceptanceToken | null = null;
  let close: { token: AcceptanceToken; afterClose: (() => void) | null } | null = null;
  return {
    begin: () => {
      current = { kind: "acceptance" };
      return current;
    },
    isCurrent: (token) => token === current,
    revoke: () => {
      current = null;
      close = null;
    },
    requestClose: (token, afterClose) => {
      if (token === current) close = { token, afterClose };
    },
    closing: () => close !== null && close.token === current,
    takeAfterClose: () => {
      const finishing = close;
      close = null;
      return finishing && finishing.token === current ? finishing.afterClose : null;
    },
  };
}
