import { describe, expect, it } from "vitest";

import { isReadOnlyHostPullRequest, readOnlyHostWriteReason } from "./readOnlyCodeHosts";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// A read-only named host, and a named host this app may change.
withPullRequestIntegrations({
  namedCodeHosts: [
    EXAMPLE_DEVICE_HOST,
    {
      ...EXAMPLE_DEVICE_HOST,
      provider: "writable-host",
      matchesHost: (host) => host === "writable.example.com",
      copy: { ...EXAMPLE_DEVICE_HOST.copy, id: "writable-host", label: "Writable Host" },
      readOnly: false,
    },
  ],
});

const DEVICE_HOST_PR = "https://2.host.example.com/example-org/app/pull/7";

describe("pull requests on a read-only named host", () => {
  it("names the host as where to change its PRs, and nothing else", () => {
    expect(isReadOnlyHostPullRequest(DEVICE_HOST_PR)).toBe(true);
    expect(readOnlyHostWriteReason(DEVICE_HOST_PR)).toBe(
      "Change this pull request on Example Host.",
    );
    for (const url of [
      "https://github.com/acme/app/pull/8",
      "https://ghe.example.com/a/b/pull/1",
      "https://writable.example.com/a/b/pull/2",
      "not a url",
    ]) {
      expect(isReadOnlyHostPullRequest(url), url).toBe(false);
      expect(readOnlyHostWriteReason(url), url).toBeNull();
    }
  });
});
