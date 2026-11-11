import { afterEach, describe, expect, it, vi } from "vitest";

import { SANDBOX_HOST_CHOICE } from "./hostPreferences";
import {
  clearProjectLaunchDefaults,
  onProjectLaunchDefaultsCleared,
  projectConfigWithLaunch,
  readProjectLaunchDefaults,
  rememberProjectHarnessMode,
  writeProjectLaunchDefaults,
  type ProjectLaunchDefaults,
} from "./projectLaunchPreferences";
import type { ProjectConfig } from "./projectsApi";

const STORAGE_KEY = "omnigent:project-launch-defaults";
const START = {
  agentId: "ag_codex",
  pickedHarness: null,
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

  it("keeps a live permission choice for the same project without changing its launch settings", () => {
    const config = { agent_id: "ag_codex" };
    writeProjectLaunchDefaults("proj_a", START, config);
    writeProjectLaunchDefaults("proj_b", START, {});

    rememberProjectHarnessMode("proj_a", "codex-native", "approve-for-me");

    expect(readProjectLaunchDefaults("proj_a", config)).toEqual({
      ...START,
      harnessOptions: { ...START.harnessOptions, mode: "approve-for-me" },
    });
    expect(readProjectLaunchDefaults("proj_b", {})).toEqual(START);
    expect(readProjectLaunchDefaults("proj_a", {})).toBeNull();
  });

  it("leaves absent projects and a different harness's settings alone", () => {
    writeProjectLaunchDefaults("proj_a", START, {});
    const saved = localStorage.getItem(STORAGE_KEY);
    rememberProjectHarnessMode("proj_missing", "codex-native", "full-access");
    rememberProjectHarnessMode("proj_a", "claude-native", "plan");
    expect(localStorage.getItem(STORAGE_KEY)).toBe(saved);
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

  it("clears one project's start and notifies listeners", () => {
    writeProjectLaunchDefaults("proj_a", START, {});
    writeProjectLaunchDefaults("proj_b", START, {});
    const listener = vi.fn();
    const unsubscribe = onProjectLaunchDefaultsCleared(listener);
    clearProjectLaunchDefaults("proj_a");
    expect(readProjectLaunchDefaults("proj_a", {})).toBeNull();
    expect(readProjectLaunchDefaults("proj_b", {})).toEqual(START);
    unsubscribe();
    clearProjectLaunchDefaults("proj_b");
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeProjectLaunchDefaults("proj_a", START, {})).not.toThrow();
    expect(() => rememberProjectHarnessMode("proj_a", "codex-native", "full-access")).not.toThrow();
    expect(readProjectLaunchDefaults("proj_a", {})).toBeNull();
  });
});

describe("projectConfigWithLaunch", () => {
  const stored = {
    host_id: "host_1",
    workspace: "/repos/alpha",
    agent_id: "ag_claude",
    model: "opus",
    effort: "low",
    use_worktree: true,
  };

  it("keeps the stored settings without a last start", () => {
    expect(projectConfigWithLaunch(stored, null)).toEqual(stored);
  });

  it.each<{ label: string; launch: ProjectLaunchDefaults; expected: ProjectConfig }>([
    {
      label: "fills the agent, model and effort from the start's options",
      launch: { agentId: "ag_codex", harnessOptions: { model: "gpt-5.6-sol", effort: "high" } },
      expected: { ...stored, agent_id: "ag_codex", model: "gpt-5.6-sol", effort: "high" },
    },
    {
      label: "shows a start on the harness defaults as no model or effort",
      launch: { agentId: "ag_codex", harnessOptions: { model: "", effort: "" } },
      expected: { ...stored, agent_id: "ag_codex", model: undefined, effort: undefined },
    },
    {
      label: "drops the stored model and effort for another agent without options",
      launch: { agentId: "ag_polly", pickedHarness: null },
      expected: { ...stored, agent_id: "ag_polly", model: undefined, effort: undefined },
    },
    {
      label: "keeps the stored model and effort for the same agent without options",
      launch: { agentId: "ag_claude", pickedHarness: null },
      expected: stored,
    },
    {
      label: "keeps the stored agent for a Smart Routing start",
      launch: { agentId: "ag_claude_wrapper", pickedHarness: "auto-native" },
      expected: stored,
    },
    {
      label: "fills the host together with its working directory",
      launch: { hostId: SANDBOX_HOST_CHOICE },
      expected: { ...stored, host_id: SANDBOX_HOST_CHOICE, workspace: undefined },
    },
  ])("$label", ({ launch, expected }) => {
    expect(projectConfigWithLaunch(stored, launch)).toEqual(expected);
  });
});
