import { describe, expect, it } from "vitest";
import {
  normalizeWorkspaceResourceTarget,
  workspaceResourceUrl,
  workspaceTargetKey,
} from "./workspaceTarget";

describe("workspace resource targets", () => {
  it("keeps string targets backward-compatible with session URLs", () => {
    expect(normalizeWorkspaceResourceTarget("sess/a")).toEqual({
      kind: "session",
      sessionId: "sess/a",
    });
    expect(workspaceResourceUrl("sess/a", "/github", { pr_url: "https://example.test/p/1" })).toBe(
      "/v1/sessions/sess%2Fa/resources/github?pr_url=https%3A%2F%2Fexample.test%2Fp%2F1",
    );
    expect(workspaceTargetKey("sess/a")).toEqual(["sess/a"]);
  });

  it("includes both host and workspace in URLs and cache keys", () => {
    const target = { kind: "host", hostId: "host/a", workspace: "/repo one" } as const;
    expect(workspaceResourceUrl(target, "environments/default/filesystem", { limit: "1000" })).toBe(
      "/v1/hosts/host%2Fa/workspace/resources/environments/default/filesystem?limit=1000&workspace=%2Frepo+one",
    );
    expect(workspaceTargetKey(target)).toEqual(["host", "host/a", "/repo one"]);
  });

  it("lets the required workspace replace a conflicting query value", () => {
    const target = { kind: "host", hostId: "h", workspace: "/selected" } as const;
    expect(workspaceResourceUrl(target, "github", { workspace: "/stale" })).toBe(
      "/v1/hosts/h/workspace/resources/github?workspace=%2Fselected",
    );
  });
});
