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
