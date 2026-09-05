import { getOmnigentTransformShareLink } from "@/lib/host";

export type SessionLinkTarget =
  | { kind: "session" }
  | { kind: "response"; responseId: string; itemId: string }
  | { kind: "item"; itemId: string };

/** Resolve a session route through the active embed basename/origin. */
export function getShareableSessionLink(
  sessionId: string,
  rebasePath: (path: string) => string,
  target: SessionLinkTarget = { kind: "session" },
): string {
  const params = new URLSearchParams();
  if (target.kind === "response") params.set("response", target.responseId);
  if (target.kind === "response") params.set("item", target.itemId);
  if (target.kind === "item") params.set("item", target.itemId);
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  const path = rebasePath(`/c/${encodeURIComponent(sessionId)}${suffix}`);
  const transform = getOmnigentTransformShareLink();
  return transform ? transform(path) : `${window.location.origin}${path}`;
}

/** Build the same-server native deep link used by desktop/mobile shells. */
export function getSessionDeepLink(
  sessionId: string,
  rebasePath: (path: string) => string,
  target: SessionLinkTarget = { kind: "session" },
): string {
  const shareable = getShareableSessionLink(sessionId, rebasePath, target);
  let host = window.location.host;
  let search = "";
  try {
    const parsed = new URL(shareable);
    host = parsed.host;
    search = parsed.search;
  } catch {
    // An embed may return a non-standard share URL; retain the current Server.
  }
  const route = target.kind === "session" ? "c" : "archive";
  return `omnigent://${host}/${route}/${encodeURIComponent(sessionId)}${search}`;
}
