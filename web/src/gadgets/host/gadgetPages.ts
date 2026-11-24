// Omnigent pages a gadget can open through the navigation methods. The frame supplies only its
// own prompt text: it never sees URLs or routing parameters, so the host appends which gadget a
// session started from it works with.
//
// Every helper returns an app-absolute path with its query (`/c/<id>`, `/?prompt=...`). Pass it to
// the router's `navigate`, which places it under the app's mount; for a new tab, rebase it first
// (`useRebasePath`) and resolve it against `window.location.origin`.

import { GADGET_FULL_SCREEN_PARAM } from "@/lib/gadgetRailTabs";
import { GADGET_SKILL_INSTRUCTION } from "./gadgetBuild";

/**
 * The viewer's prompt followed by a note that names the gadget and points to the gadget skill,
 * so the session's `sys_gadget_*` tools reach its records.
 */
export function composeGadgetContextPrompt(
  prompt: string,
  app: { id: string; name: string },
): string {
  return [
    prompt.trim(),
    "",
    "---",
    `Omnigent added this note because the session was started from the gadget ${JSON.stringify(app.name)} (gadget_id ${app.id}). To work with its records: ${GADGET_SKILL_INSTRUCTION}`,
  ].join("\n");
}

/** A gadget's own page. New sessions started from it return under it. */
export function gadgetBoardPath(appId: string): string {
  return `/gadget/${encodeURIComponent(appId)}`;
}

/**
 * The query parameter that marks New session as started from a gadget. The session then opens at
 * its usual path and the page adopts it there as the gadget's session.
 */
export const GADGET_START_PARAM = "gadget_start";

/**
 * The session ID in a path under `sessionPrefix` (`/c/` rebased), or null for any other page.
 * A segment that isn't valid percent-encoding is kept as written, as `useActiveConversationId`
 * does, so a malformed URL can't break rendering.
 */
export function sessionIdFromPath(pathname: string, sessionPrefix: string): string | null {
  if (!pathname.startsWith(sessionPrefix)) return null;
  const segment = pathname.slice(sessionPrefix.length).replace(/\/$/, "");
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

function withQuery(path: string, params: Record<string, string | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) {
      query.set(key, value);
    }
  }
  const search = query.toString();
  return search ? `${path}?${search}` : path;
}

/**
 * New session started from the gadget, with the composer prefilled. The created session is then
 * linked to the gadget (see `useAdoptStartedGadgetSession`). The gadget beside it fills the screen
 * only with `fullScreen`; a link with a prompt and without it shows the composer.
 */
export function gadgetBoardNewSessionUrl(
  appId: string,
  prompt: string,
  { fullScreen = false }: { fullScreen?: boolean } = {},
): string {
  return withQuery("/", {
    prompt,
    [GADGET_START_PARAM]: appId,
    [GADGET_FULL_SCREEN_PARAM]: fullScreen ? appId : undefined,
  });
}

/** New session with the composer prefilled; the viewer still chooses where it runs and starts it. */
export function gadgetNewSessionUrl(prompt: string): string {
  return withQuery("/", { prompt });
}

/** Automations with the create dialog open and prefilled; the viewer reviews and saves it. */
export function gadgetNewAutomationUrl({
  name,
  prompt,
  schedule,
}: {
  name: string;
  prompt: string;
  schedule?: string;
}): string {
  return withQuery("/tasks", { new: "1", name, prompt, schedule });
}

export function gadgetSessionUrl(sessionId: string): string {
  return `/c/${encodeURIComponent(sessionId)}`;
}
