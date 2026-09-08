import { afterEach, describe, expect, it, vi } from "vitest";
import { getUserSettings, updateUserSettings } from "./userSettingsApi";

describe("userSettingsApi", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reads the current user's settings", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ background_session_titles_enabled: true }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getUserSettings()).resolves.toEqual({
      backgroundSessionTitlesEnabled: true,
    });
    expect(fetchMock).toHaveBeenCalledWith("/v1/user-settings", { cache: "no-store" });
  });

  it("updates the current user's setting", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ background_session_titles_enabled: false }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(updateUserSettings({ backgroundSessionTitlesEnabled: false })).resolves.toEqual({
      backgroundSessionTitlesEnabled: false,
    });
    expect(fetchMock).toHaveBeenCalledWith("/v1/user-settings", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ background_session_titles_enabled: false }),
    });
  });
});
