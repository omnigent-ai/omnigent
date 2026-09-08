export interface UserSettings {
  backgroundSessionTitlesEnabled: boolean;
}

interface UserSettingsResponse {
  background_session_titles_enabled: boolean;
}

function fromResponse(settings: UserSettingsResponse): UserSettings {
  return {
    backgroundSessionTitlesEnabled: settings.background_session_titles_enabled,
  };
}

export async function getUserSettings(): Promise<UserSettings> {
  const response = await fetch("/v1/user-settings", { cache: "no-store" });
  if (!response.ok) throw new Error("Failed to load user settings");
  return fromResponse((await response.json()) as UserSettingsResponse);
}

export async function updateUserSettings(settings: UserSettings): Promise<UserSettings> {
  const response = await fetch("/v1/user-settings", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      background_session_titles_enabled: settings.backgroundSessionTitlesEnabled,
    }),
  });
  if (!response.ok) throw new Error("Failed to update user settings");
  return fromResponse((await response.json()) as UserSettingsResponse);
}
