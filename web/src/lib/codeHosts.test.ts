import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { GIT_PROVIDERS } from "@/lib/gitProviders";
import {
  type NamedCodeHost,
  type PullRequestIntegrations,
  pullRequestIntegrations,
  registerPullRequestIntegrations,
  restorePullRequestIntegrations,
} from "@/lib/pullRequestIntegrations";

import {
  codeHostProvider,
  isDeviceReadHost,
  isDeviceReadProvider,
  isMacReadProvider,
  namedCodeHostOf,
  parseCodeHostPullRequestUrl,
} from "./codeHosts";

// A named host serving numbered servers under host.example.com.
const EXAMPLE_NAMED_HOST: NamedCodeHost = {
  provider: "named-host",
  matchesHost: (host) => /^[1-9][0-9]*\.host\.example\.com$/.test(host),
  copy: { ...GIT_PROVIDERS.github, id: "named-host", label: "Example Host" },
  hostedOnGitHub: true,
  readsAsDevice: true,
  readOnly: true,
  linksOnDevice: true,
};

const PULL_REQUEST = "https://github.com/example-org/app/pull/1234";

describe("codeHostProvider", () => {
  it("names github.com from the host alone, in any case", () => {
    expect(codeHostProvider("github.com")).toBe("github");
    expect(codeHostProvider("GitHub.com")).toBe("github");
  });

  it("treats an Enterprise host as GitHub only with evidence", () => {
    expect(codeHostProvider("ghe.example.com")).toBeNull();
    expect(codeHostProvider("ghe.example.com", ["GHE.example.com"])).toBe("github");
  });

  it("doesn't recognize a host that only looks like github.com", () => {
    expect(codeHostProvider("github.com.evil.example")).toBeNull();
    expect(codeHostProvider("evil-github.com")).toBeNull();
  });
});

describe("parseCodeHostPullRequestUrl", () => {
  it("reads a github.com pull request", () => {
    expect(parseCodeHostPullRequestUrl(PULL_REQUEST)).toEqual({
      provider: "github",
      host: "github.com",
      repository: "example-org/app",
      number: 1234,
      url: PULL_REQUEST,
    });
  });

  it("drops a query, fragment, and trailing slash", () => {
    expect(parseCodeHostPullRequestUrl(`${PULL_REQUEST}/?w=1#top`)?.url).toBe(PULL_REQUEST);
  });

  it("reads an Enterprise host only with evidence", () => {
    expect(parseCodeHostPullRequestUrl("https://ghe.example.com/o/r/pull/7")).toBeNull();
    expect(
      parseCodeHostPullRequestUrl("https://ghe.example.com/o/r/pull/7", ["ghe.example.com"]),
    ).toMatchObject({ provider: "github", host: "ghe.example.com", repository: "o/r", number: 7 });
  });

  it.each([
    ["nothing", null],
    ["not a link", "example-org/app#1234"],
    ["plain http", "http://github.com/o/r/pull/7"],
    ["a port", "https://github.com:8443/o/r/pull/7"],
    ["credentials", "https://user:secret@github.com/o/r/pull/7"],
    ["an issue", "https://github.com/o/r/issues/7"],
    ["a pull request's files tab", "https://github.com/o/r/pull/7/files"],
    ["no number", "https://github.com/o/r/pull/x"],
  ])("reads nothing from %s", (_case, url) => {
    expect(parseCodeHostPullRequestUrl(url)).toBeNull();
  });
});

describe("codeHostProvider with a registered named host", () => {
  let saved: PullRequestIntegrations;
  beforeEach(() => {
    saved = pullRequestIntegrations();
  });
  afterEach(() => restorePullRequestIntegrations(saved));

  it("names only github.com while no named host is registered", () => {
    expect(codeHostProvider("1.host.example.com")).toBeNull();
    expect(isDeviceReadHost("1.host.example.com")).toBe(false);
  });

  it("names a registered host's provider, in any case, ahead of GitHub Enterprise", () => {
    registerPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_NAMED_HOST] });
    expect(codeHostProvider("1.Host.example.com", ["1.host.example.com"])).toBe("named-host");
    expect(isDeviceReadHost("1.host.example.com")).toBe(true);
    expect(codeHostProvider("github.com")).toBe("github");
  });
});

describe("provider lookups", () => {
  let saved: PullRequestIntegrations;
  beforeEach(() => {
    saved = pullRequestIntegrations();
  });
  afterEach(() => restorePullRequestIntegrations(saved));

  const SERVER_READ_HOST: NamedCodeHost = {
    ...EXAMPLE_NAMED_HOST,
    provider: "server-read",
    readsAsDevice: false,
  };

  it.each<[string | null | undefined, boolean, boolean, boolean]>([
    // provider, registered, reads as this Mac, read on this Mac at all
    ["github", false, false, true],
    ["named-host", true, true, true],
    ["server-read", true, false, false],
    ["unknown", false, false, false],
    ["", false, false, false],
    [null, false, false, false],
    [undefined, false, false, false],
  ])("reads %j: registered %s, as the Mac %s, on the Mac %s", (provider, named, device, mac) => {
    registerPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_NAMED_HOST, SERVER_READ_HOST] });
    expect(namedCodeHostOf(provider) !== null).toBe(named);
    expect(isDeviceReadProvider(provider)).toBe(device);
    expect(isMacReadProvider(provider)).toBe(mac);
  });

  it("finds no provider while nothing is registered", () => {
    expect(namedCodeHostOf("named-host")).toBeNull();
    expect(isDeviceReadProvider("named-host")).toBe(false);
    expect(isMacReadProvider("named-host")).toBe(false);
    expect(isMacReadProvider("github")).toBe(true);
  });
});
