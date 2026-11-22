import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PullRequestAssociation } from "@/hooks/usePullRequests";

import { useStoredPullRequestFallback } from "./useStoredPullRequestFallback";

const state = vi.hoisted(() => ({
  storesPrs: true,
  hostOffline: true,
  stored: undefined as { prs: PullRequestAssociation[]; synced: boolean } | undefined,
}));

vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({}) }));
vi.mock("@/hooks/useSessionPullRequests", () => ({
  storesSessionPullRequests: () => state.storesPrs,
  useSessionPullRequests: () => ({ data: state.stored }),
}));
vi.mock("@/shell/PullRequestPanel", () => ({ useHostOffline: () => state.hostOffline }));

const storedPr = (url: string): PullRequestAssociation => ({
  url,
  host: new URL(url).host,
  repository: url.split("/").slice(3, 5).join("/"),
  number: Number(url.split("/").at(-1)),
  title: "Ship it",
  relationship: "created",
});

const fallback = (url: string) =>
  renderHook(() => useStoredPullRequestFallback("conv_1", { data: undefined, error: null }, url))
    .result.current;

beforeEach(() => {
  state.storesPrs = true;
  state.hostOffline = true;
  state.stored = undefined;
});

describe("useStoredPullRequestFallback", () => {
  it("is the server's copy of the pull request while the host is offline", () => {
    const one = "https://github.com/example/one/pull/42";
    const two = "https://github.com/example/two/pull/7";
    state.stored = { prs: [storedPr(one), storedPr(two)], synced: true };
    expect(fallback(two)).toEqual({ storedPr: storedPr(two), notSynced: false });
  });

  it("finds the stored copy however the link's owner and repository are cased", () => {
    const stored = "https://github.com/Example-Org/Widgets/pull/42";
    state.stored = { prs: [storedPr(stored)], synced: true };
    expect(fallback("https://github.com/example-org/widgets/pull/42").storedPr?.url).toBe(stored);
    expect(fallback("https://GitHub.com/EXAMPLE-ORG/widgets/pull/42/").storedPr?.url).toBe(stored);
  });

  it("never takes another pull request, repository, host or port for it", () => {
    state.stored = {
      prs: [storedPr("https://ghe.example:8443/example-org/widgets/pull/42")],
      synced: true,
    };
    for (const url of [
      "https://ghe.example:8443/example-org/widgets/pull/43",
      "https://ghe.example:8443/example-org/gadgets/pull/42",
      "https://ghe.example/example-org/widgets/pull/42",
      "https://other.example:8443/example-org/widgets/pull/42",
    ]) {
      expect(fallback(url).storedPr).toBeUndefined();
    }
  });

  it("says the server hasn't imported the session's PRs when it has no copy yet", () => {
    state.stored = { prs: [], synced: false };
    expect(fallback("https://github.com/example/one/pull/42")).toEqual({
      storedPr: undefined,
      notSynced: true,
    });
  });

  it("is nothing while the host is online, or on a server that doesn't store PRs", () => {
    const one = "https://github.com/example/one/pull/42";
    state.stored = { prs: [storedPr(one)], synced: false };
    state.hostOffline = false;
    expect(fallback(one)).toEqual({ storedPr: undefined, notSynced: false });
    state.hostOffline = true;
    state.storesPrs = false;
    expect(fallback(one)).toEqual({ storedPr: undefined, notSynced: false });
  });
});
