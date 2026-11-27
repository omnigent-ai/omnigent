import { afterEach, describe, expect, it } from "vitest";
import {
  type NamedCodeHost,
  pullRequestIntegrations,
  registerPullRequestIntegrations,
  restorePullRequestIntegrations,
} from "@/lib/pullRequestIntegrations";
import type { StackFormat } from "@/lib/pullRequestStacks/stackModel";

const saved = pullRequestIntegrations();
afterEach(() => restorePullRequestIntegrations(saved));

const host = (provider: string) => ({ provider }) as unknown as NamedCodeHost;
const format = (id: string): StackFormat => ({ id, parse: () => null });

describe("registerPullRequestIntegrations", () => {
  it("replaces a host or stack format registered again, keeping its place", () => {
    registerPullRequestIntegrations({
      namedCodeHosts: [host("first"), host("second")],
      stackFormats: [format("list"), format("table")],
    });
    const again = format("list");
    registerPullRequestIntegrations({
      namedCodeHosts: [host("first")],
      stackFormats: [again, format("tree")],
    });
    const { namedCodeHosts, stackFormats } = pullRequestIntegrations();
    expect(namedCodeHosts.map((entry) => entry.provider)).toEqual(["first", "second"]);
    expect(stackFormats.map((entry) => entry.id)).toEqual(["list", "table", "tree"]);
    expect(stackFormats[0]).toBe(again);
  });
});
