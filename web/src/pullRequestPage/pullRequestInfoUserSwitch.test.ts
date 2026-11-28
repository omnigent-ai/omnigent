import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  user: "alice" as string | null,
  listeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", () => ({
  getCurrentUserId: () => identity.user,
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.add(listener);
    return () => identity.listeners.delete(listener);
  },
}));

import {
  resetPullRequestInfoOnUserSwitch,
  stopPullRequestInfoUserSwitchForTests,
} from "./pullRequestInfoUserSwitch";

const OWNER_INFO = { auth: { accounts: [{ login: "owner-personal" }] } };

function signInAs(user: string | null) {
  identity.user = user;
  for (const listener of identity.listeners) listener();
}

describe("resetPullRequestInfoOnUserSwitch", () => {
  const client = new QueryClient();

  afterEach(() => {
    stopPullRequestInfoUserSwitchForTests(client);
    client.clear();
    identity.user = "alice";
  });

  function seed() {
    client.setQueryData(["pr-page-github-info", "session-1"], OWNER_INFO);
    client.setQueryData(["github-info", "session-1"], OWNER_INFO);
    client.setQueryData(["sessions", "session-1"], { title: "Kept" });
  }

  it("drops both pages' info when another user signs in, and nothing else", () => {
    resetPullRequestInfoOnUserSwitch(client);
    seed();

    signInAs("bob");

    expect(client.getQueryData(["pr-page-github-info", "session-1"])).toBeUndefined();
    expect(client.getQueryData(["github-info", "session-1"])).toBeUndefined();
    expect(client.getQueryData(["sessions", "session-1"])).toEqual({ title: "Kept" });
  });

  it("keeps the info when the same user is confirmed again", () => {
    resetPullRequestInfoOnUserSwitch(client);
    seed();

    signInAs("alice");

    expect(client.getQueryData(["pr-page-github-info", "session-1"])).toEqual(OWNER_INFO);
  });

  it("watches once however many readers install it", () => {
    resetPullRequestInfoOnUserSwitch(client);
    resetPullRequestInfoOnUserSwitch(client);
    expect(identity.listeners.size).toBe(1);
  });
});
