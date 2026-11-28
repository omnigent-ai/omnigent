import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchPullRequestSuggestions,
  PULL_REQUEST_SUGGESTIONS_OPERATION,
  PullRequestSuggestionsError,
} from "./pullRequestSuggestions";

const ACCOUNT = { host: "github.com", login: "octo" };

function installShell(githubQuery: (...args: unknown[]) => unknown) {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    githubQuery,
    getDesktopFeatures: async () => ({
      githubFastTrack: true,
      githubFastTrackOperations: ["account_status", PULL_REQUEST_SUGGESTIONS_OPERATION],
    }),
  };
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("fetchPullRequestSuggestions", () => {
  it("asks for the session's repository, or for none", async () => {
    const data = { repository: null, mine: [], recent: [] };
    const githubQuery = vi.fn().mockResolvedValue({ ok: true, data });
    installShell(githubQuery);
    await expect(fetchPullRequestSuggestions(ACCOUNT, "acme/app")).resolves.toBe(data);
    await fetchPullRequestSuggestions(ACCOUNT, null);
    expect(githubQuery.mock.calls).toEqual([
      [PULL_REQUEST_SUGGESTIONS_OPERATION, ACCOUNT, { repository: "acme/app" }],
      [PULL_REQUEST_SUGGESTIONS_OPERATION, ACCOUNT, {}],
    ]);
  });

  it("fails with the fast track's message, and as unsupported outside the shell", async () => {
    installShell(async () => ({
      ok: false,
      error: { code: "query_failed", message: "GitHub couldn't answer. Try again in a moment." },
    }));
    const failure = fetchPullRequestSuggestions(ACCOUNT, "acme/app");
    await expect(failure).rejects.toBeInstanceOf(PullRequestSuggestionsError);
    await expect(failure).rejects.toMatchObject({
      code: "query_failed",
      message: "GitHub couldn't answer. Try again in a moment.",
    });
    delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    await expect(fetchPullRequestSuggestions(ACCOUNT, null)).rejects.toMatchObject({
      code: "unsupported",
    });
  });
});
