import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { canvasPullRequest, usePullRequests } from "./pullRequests";

const FORGE_DISPLAY = {
  id: "example_forge",
  display_name: "Example Forge",
  request_name: "pull request",
  number_prefix: "!",
};

const FORGE_URL = "https://forge.example.test/acme/proj/_git/repo/pullrequest/7";

function session(id: string, pullRequest: Conversation["pull_request"]): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 1,
    updated_at: 1,
    labels: {},
    permission_level: null,
    pull_request: pullRequest,
  };
}

describe("canvasPullRequest", () => {
  it("shows the session's stored first PR with its own provider", () => {
    expect(
      canvasPullRequest(
        session("s1", {
          url: FORGE_URL,
          provider: "example_forge",
          provider_display: FORGE_DISPLAY,
          repository: "acme/proj/repo",
          number: 7,
          title: "Ship it",
          state: "MERGED",
          is_draft: false,
        }),
      ),
    ).toEqual({
      number: 7,
      title: "Ship it",
      state: "MERGED",
      url: FORGE_URL,
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
    });
  });

  it("names the repository until the host fetches the title and state", () => {
    expect(
      canvasPullRequest(
        session("s1", {
          url: "https://github.com/example/one/pull/42",
          provider: "github",
          repository: "example/one",
          number: 42,
          title: null,
          state: null,
        }),
      ),
    ).toMatchObject({ title: "example/one", state: null, provider_display: null });
  });

  it("ignores sessions without a PR and PRs without an https URL", () => {
    expect(canvasPullRequest(session("s1", undefined))).toBeNull();
    expect(
      canvasPullRequest(
        session("s2", {
          url: "javascript:alert(1)",
          provider: "github",
          repository: "example/one",
          number: 1,
        }),
      ),
    ).toBeNull();
  });
});

describe("usePullRequests", () => {
  it("keys each session's card PR by id and keeps the map while sessions are unchanged", () => {
    const sessions = [
      session("s1", {
        url: FORGE_URL,
        provider: "example_forge",
        repository: "acme/proj/repo",
        number: 7,
        title: "Ship it",
        state: "OPEN",
      }),
      session("s2", null),
    ];
    const { result, rerender } = renderHook(({ items }) => usePullRequests(items), {
      initialProps: { items: sessions },
    });
    const first = result.current;
    expect(Object.keys(first)).toEqual(["s1", "s2"]);
    expect(first.s1?.number).toBe(7);
    expect(first.s2).toBeNull();
    rerender({ items: sessions });
    expect(result.current).toBe(first);
  });
});
