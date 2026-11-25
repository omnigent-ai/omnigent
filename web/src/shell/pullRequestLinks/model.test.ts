import { describe, expect, it } from "vitest";

import { AVAILABLE, HIDDEN, unavailableBecause } from "@/store/paletteCommands";

import {
  ALREADY_LINKED_REASON,
  type LinkToSessionFacts,
  linkToSessionAvailability,
  chatPullRequestTarget,
  LINK_PENDING_REASON,
} from "./model";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

const SPOKE_PR = "https://2.spoke.example.com/example-org/universe/pull/20002346";

describe("chatPullRequestTarget", () => {
  it("names a github.com or GitSpoke PR by its canonical link and number", () => {
    expect(chatPullRequestTarget("https://github.com/acme/app/pull/12?w=1#top")).toEqual({
      url: "https://github.com/acme/app/pull/12",
      number: 12,
      title: null,
      provider: "github",
    });
    expect(chatPullRequestTarget(SPOKE_PR)).toMatchObject({
      url: SPOKE_PR,
      number: 20002346,
      provider: "gitspoke",
    });
  });

  it.each([
    "https://github.com/acme/app/issues/12",
    "https://github.com/acme/app",
    "http://github.com/acme/app/pull/12",
    "https://0.spoke.example.com/o/r/pull/1",
    "https://gitlab.com/acme/app/-/merge_requests/3",
    "/c/conv_1",
    undefined,
  ])("leaves %s alone", (href) => {
    expect(chatPullRequestTarget(href)).toBeNull();
  });
});

describe("linkToSessionAvailability", () => {
  const ready: LinkToSessionFacts = {
    access: AVAILABLE,
    linked: false,
    pending: false,
    hostUnavailableReason: null,
    linksOnThisMac: false,
  };
  const offline = "To link a PR, please reconnect the session’s host.";

  it.each<[string, Partial<LinkToSessionFacts>, ReturnType<typeof unavailableBecause>]>([
    ["can link", {}, AVAILABLE],
    ["has no session", { access: HIDDEN, linked: true }, HIDDEN],
    [
      "lacks edit access, before anything else",
      { access: unavailableBecause("No edit."), linked: true, pending: true },
      unavailableBecause("No edit."),
    ],
    [
      "already links it",
      { linked: true, pending: true },
      unavailableBecause(ALREADY_LINKED_REASON),
    ],
    ["is linking another", { pending: true }, unavailableBecause(LINK_PENDING_REASON)],
    [
      "needs its host, which is away",
      { hostUnavailableReason: offline },
      unavailableBecause(offline),
    ],
    [
      "keeps it on this Mac while the host is away",
      { hostUnavailableReason: offline, linksOnThisMac: true },
      AVAILABLE,
    ],
  ])("when the session %s", (_label, facts, expected) => {
    expect(linkToSessionAvailability({ ...ready, ...facts })).toEqual(expected);
  });
});
