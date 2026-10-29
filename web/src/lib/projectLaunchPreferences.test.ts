import { afterEach, describe, expect, it, vi } from "vitest";

import { readProjectLaunchDefaults, writeProjectLaunchDefaults } from "./projectLaunchPreferences";

const STORAGE_KEY = "omnigent:project-launch-defaults";
const START = {
  agentId: "ag_codex",
  harness: "codex-native",
  harnessOptions: { model: "gpt-5.6-sol", effort: "high", mode: "on-request" },
  hostId: "host_1",
  workspace: "/repos/working-dir-b",
};

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("projectLaunchPreferences", () => {
  it("returns null when nothing is stored", () => {
    expect(readProjectLaunchDefaults("proj_a", {})).toBeNull();
  });

  it("round-trips a start per project", () => {
    writeProjectLaunchDefaults("proj_a", START, {});
    writeProjectLaunchDefaults("proj_b", { agentId: "ag_claude", hostId: "host_2" }, {});
    expect(readProjectLaunchDefaults("proj_a", {})).toEqual(START);
    expect(readProjectLaunchDefaults("proj_b", {})).toEqual({
      agentId: "ag_claude",
      hostId: "host_2",
    });
  });

  it("drops a start once the project's stored defaults change", () => {
    writeProjectLaunchDefaults("proj_a", START, { agent_id: "ag_claude" });
    expect(readProjectLaunchDefaults("proj_a", { agent_id: "ag_claude" })).toEqual(START);
    expect(readProjectLaunchDefaults("proj_a", { agent_id: "ag_pi" })).toBeNull();
    expect(readProjectLaunchDefaults("proj_a", {})).toBeNull();
  });

  it("keeps a start across key-order and icon-only config changes", () => {
    writeProjectLaunchDefaults("proj_a", START, { agent_id: "ag_claude", model: "opus" });
    expect(
      readProjectLaunchDefaults("proj_a", { model: "opus", agent_id: "ag_claude", icon: "🔥" }),
    ).toEqual(START);
  });

  it("ignores a malformed entry and overwrites a corrupt store", () => {
    writeProjectLaunchDefaults("proj_a", START, {});
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    stored.proj_a.harnessOptions.effort = 7;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
    expect(readProjectLaunchDefaults("proj_a", {})).toBeNull();

    localStorage.setItem(STORAGE_KEY, "{not json");
    expect(readProjectLaunchDefaults("proj_a", {})).toBeNull();
    writeProjectLaunchDefaults("proj_a", START, {});
    expect(readProjectLaunchDefaults("proj_a", {})).toEqual(START);
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeProjectLaunchDefaults("proj_a", START, {})).not.toThrow();
    expect(readProjectLaunchDefaults("proj_a", {})).toBeNull();
  });
});
