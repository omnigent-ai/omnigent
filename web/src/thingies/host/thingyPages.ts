// Omnigent pages a thingy can open through the navigation methods. The frame supplies only its
// own prompt text: it never sees URLs or routing parameters, so the host appends which thingy a
// session started from it works with.
//
// Every helper returns an app-absolute path with its query (`/c/<id>`, `/?prompt=...`). Pass it to
// the router's `navigate`, which places it under the app's mount; for a new tab, rebase it first
// (`useRebasePath`) and resolve it against `window.location.origin`.

import { THINGY_FULL_SCREEN_PARAM } from "@/lib/thingyRailTabs";
import { THINGY_SKILL_INSTRUCTION } from "./thingyBuild";

/**
 * The viewer's prompt followed by a note that names the thingy and points to the thingy skill,
 * so the session's `sys_thingy_*` tools reach its records.
 */
export function composeThingyContextPrompt(
  prompt: string,
  app: { id: string; name: string },
): string {
  return [
    prompt.trim(),
    "",
    "---",
    `Omnigent added this note because the session was started from the thingy ${JSON.stringify(app.name)} (thingy_id ${app.id}). To work with its records: ${THINGY_SKILL_INSTRUCTION}`,
  ].join("\n");
}

/** A thingy's own page. New sessions started from it return under it. */
export function thingyBoardPath(appId: string): string {
  return `/thingy/${encodeURIComponent(appId)}`;
}

/**
 * The query parameter that marks New session as started from a thingy. The session then opens at
 * its usual path and the page adopts it there as the thingy's session.
 */
export const THINGY_START_PARAM = "thingy_start";

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
 * New session started from the thingy, with the composer prefilled. The created session is then
 * linked to the thingy (see `useAdoptStartedThingySession`). The thingy beside it fills the screen
 * only with `fullScreen`; a link with a prompt and without it shows the composer.
 */
export function thingyBoardNewSessionUrl(
  appId: string,
  prompt: string,
  { fullScreen = false }: { fullScreen?: boolean } = {},
): string {
  return withQuery("/", {
    prompt,
    [THINGY_START_PARAM]: appId,
    [THINGY_FULL_SCREEN_PARAM]: fullScreen ? appId : undefined,
  });
}

/** New session with the composer prefilled; the viewer still chooses where it runs and starts it. */
export function thingyNewSessionUrl(prompt: string): string {
  return withQuery("/", { prompt });
}

/** Automations with the create dialog open and prefilled; the viewer reviews and saves it. */
export function thingyNewAutomationUrl({
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

export function thingySessionUrl(sessionId: string): string {
  return `/c/${encodeURIComponent(sessionId)}`;
}
