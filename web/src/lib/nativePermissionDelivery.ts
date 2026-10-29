export interface NativePermissionDelivery {
  state: "ready" | "sending" | "unconfirmed";
  attempt: string;
  action?: "accept" | "decline" | "cancel";
}

export function parseNativePermissionDelivery(
  value: unknown,
): NativePermissionDelivery | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const raw = value as Record<string, unknown>;
  if (typeof raw.attempt !== "string" || !raw.attempt) return undefined;
  if (raw.state !== "ready" && raw.state !== "sending" && raw.state !== "unconfirmed")
    return undefined;
  const action = raw.action;
  if (raw.state !== "ready" && action !== "accept" && action !== "decline" && action !== "cancel")
    return undefined;
  return {
    state: raw.state,
    attempt: raw.attempt,
    ...(action === "accept" || action === "decline" || action === "cancel" ? { action } : {}),
  };
}

/** How often pages re-read a prompt while a native agent re-delivers it. */
export const NATIVE_REPARK_POLL_MS = 1000;
/** A terminal hold lasts until the user answers there, so pages poll it more slowly. */
export const TERMINAL_HOLD_POLL_MS = 5000;
