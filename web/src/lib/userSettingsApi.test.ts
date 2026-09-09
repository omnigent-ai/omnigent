import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import { getUserSettings, updateUserSettings } from "./userSettingsApi";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

describe("userSettingsApi", () => {
  afterEach(() => vi.clearAllMocks());

  it("reads the current user's settings", async () => {
    const fetchMock = vi
      .mocked(authenticatedFetch)
      .mockResolvedValue(
        new Response(JSON.stringify({ background_session_titles_enabled: true }), { status: 200 }),
      );

    await expect(getUserSettings()).resolves.toEqual({
      backgroundSessionTitlesEnabled: true,
    });
    expect(fetchMock).toHaveBeenCalledWith("/v1/user-settings", { cache: "no-store" });
  });

  it("updates the current user's setting", async () => {
    const fetchMock = vi
      .mocked(authenticatedFetch)
      .mockResolvedValue(
        new Response(JSON.stringify({ background_session_titles_enabled: false }), { status: 200 }),
      );

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
