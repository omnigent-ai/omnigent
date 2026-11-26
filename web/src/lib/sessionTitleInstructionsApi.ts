// Typed client for the caller's saved session title instructions
// (`omnigent/server/routes/user_settings.py`). The server adds them to the
// prompt it uses to name the caller's sessions.

import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

const SESSION_TITLE_INSTRUCTIONS_PATH = "/v1/settings/session-title-instructions";

export interface SessionTitleInstructions {
  /** Saved guidance, or null when only the server or default title prompt applies. */
  instructions: string | null;
  /** Longest guidance the server accepts, in characters. */
  max_chars: number;
}

export async function getSessionTitleInstructions(): Promise<SessionTitleInstructions> {
  const res = await authenticatedFetch(SESSION_TITLE_INSTRUCTIONS_PATH);
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as SessionTitleInstructions;
}

/** Save the guidance; null or blank clears it. */
export async function saveSessionTitleInstructions(
  instructions: string | null,
): Promise<SessionTitleInstructions> {
  const res = await authenticatedFetch(SESSION_TITLE_INSTRUCTIONS_PATH, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instructions }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as SessionTitleInstructions;
}
