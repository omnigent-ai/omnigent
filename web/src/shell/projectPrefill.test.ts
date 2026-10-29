// Pure state-machine tests for the project-prefill config seeding. The
// component-level rules live in NewChatDialog.projectPrefill.test.tsx; these
// pin the transitions that need mid-flight timing (a user acting between the
// config loading and resolving), which the rendered harness can't sequence.
import { describe, expect, it } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { SANDBOX_HOST_CHOICE } from "@/lib/hostPreferences";
import type { ProjectLaunchDefaults } from "@/lib/projectLaunchPreferences";
import {
  initialPrefillState,
  projectPrefillStep,
  withProjectLaunchDefaults,
  type ProjectPrefillConfig,
} from "./projectPrefill";

const hosts: Host[] = [
  { host_id: "host_1", name: "laptop", owner: "corey", status: "online" },
  { host_id: "host_2", name: "desktop", owner: "corey", status: "online" },
];

function inputs(overrides: Partial<Parameters<typeof projectPrefillStep>[1]> = {}) {
  return {
    hosts,
    agents: [{ id: "ag_hello" }],
    sandboxSelected: false,
    managedSandboxesEnabled: false,
    selectedHostId: null,
    lastAgentId: null,
    config: {},
    ...overrides,
  };
}

/** Run the machine from the start until it settles (or stalls). */
function runToDone(stepInputs: ReturnType<typeof inputs>) {
  let state = initialPrefillState("Alpha");
  const writes: Record<string, string | boolean> = {};
  for (let i = 0; i < 10; i++) {
    const step = projectPrefillStep(state, stepInputs);
    if (step === null) break;
    state = step.state;
    Object.assign(writes, step.writes);
    if (state.phase === "settled" && state.agentSeeded) break;
  }
  return { state, writes };
}

describe("projectPrefill config seeding", () => {
  it("waits while the config is still loading", () => {
    const step = projectPrefillStep(initialPrefillState("Alpha"), inputs({ config: undefined }));
    expect(step).toBeNull();
  });

  it("keeps the location phase open while the host list is still loading", () => {
    const step = projectPrefillStep(
      initialPrefillState("Alpha"),
      inputs({ hosts: undefined, config: { hostId: "host_1" } }),
    );
    // The agent track can settle independently, but the host seed must wait
    // for the host list, so the location phase stays open (no host write).
    expect(step).not.toBeNull();
    expect(step!.state.phase).toBe("location");
    expect(step!.writes.hostId).toBeUndefined();
  });

  it("seeds host + workspace from config", () => {
    const { state, writes } = runToDone(
      inputs({ config: { hostId: "host_2", workspace: "/repo/beta" } }),
    );
    expect(writes.hostId).toBe("host_2");
    expect(writes.workspace).toBe("/repo/beta");
    expect(state.phase).toBe("settled");
    expect(state.agentSeeded).toBe(true);
  });

  it("seeds only the host when config has a host but no workspace", () => {
    const { writes } = runToDone(inputs({ config: { hostId: "host_2" } }));
    expect(writes.hostId).toBe("host_2");
    expect(writes.workspace).toBeUndefined();
  });

  it("settles with no location writes when the config is empty", () => {
    const { state, writes } = runToDone(inputs({ config: {} }));
    expect(state.phase).toBe("settled");
    expect(writes.hostId).toBeUndefined();
    expect(writes.workspace).toBeUndefined();
  });

  it("drops an offline / missing config host but keeps the configured workspace", () => {
    const { writes } = runToDone(inputs({ config: { hostId: "host_off", workspace: "/x" } }));
    // The offline config host is ignored (the generic host default takes
    // over), but the workspace hint survives — otherwise a generic recent
    // path, possibly another project's, would fill the field.
    expect(writes.hostId).toBeUndefined();
    expect(writes.workspace).toBe("/x");
  });

  it("seeds a configured workspace even when the config names no host", () => {
    const { writes } = runToDone(inputs({ config: { workspace: "/x" } }));
    expect(writes.hostId).toBeUndefined();
    expect(writes.workspace).toBe("/x");
  });

  it("does not seed a config host the user has already switched away from", () => {
    const { writes } = runToDone(
      inputs({ config: { hostId: "host_2", workspace: "/repo/beta" }, selectedHostId: "host_1" }),
    );
    expect(writes.hostId).toBeUndefined();
    expect(writes.workspace).toBeUndefined();
  });

  it("does not seed a config host when the sandbox is selected", () => {
    const { writes } = runToDone(
      inputs({ config: { hostId: "host_1", workspace: "/repo" }, sandboxSelected: true }),
    );
    expect(writes.hostId).toBeUndefined();
    expect(writes.workspace).toBeUndefined();
  });

  it("selects the sandbox from a stored sandbox default", () => {
    const { state, writes } = runToDone(
      inputs({ config: { hostId: SANDBOX_HOST_CHOICE }, managedSandboxesEnabled: true }),
    );
    // The sandbox sentinel is not a real host id, so it seeds via selectSandbox.
    expect(writes.selectSandbox).toBe(true);
    expect(writes.hostId).toBeUndefined();
    expect(state.phase).toBe("settled");
  });

  it("drops a stored sandbox default when the server no longer offers sandboxes", () => {
    const { writes } = runToDone(
      inputs({ config: { hostId: SANDBOX_HOST_CHOICE }, managedSandboxesEnabled: false }),
    );
    expect(writes.selectSandbox).toBeUndefined();
    expect(writes.hostId).toBeUndefined();
  });

  it("does not re-select the sandbox once it is already selected", () => {
    const { writes } = runToDone(
      inputs({
        config: { hostId: SANDBOX_HOST_CHOICE },
        managedSandboxesEnabled: true,
        sandboxSelected: true,
      }),
    );
    expect(writes.selectSandbox).toBeUndefined();
  });

  it("does not select the sandbox when the user already picked a host", () => {
    const { writes } = runToDone(
      inputs({
        config: { hostId: SANDBOX_HOST_CHOICE },
        managedSandboxesEnabled: true,
        selectedHostId: "host_1",
      }),
    );
    expect(writes.selectSandbox).toBeUndefined();
  });
});

describe("projectPrefill agent seeding", () => {
  it("seeds the agent from config when it is a pickable agent", () => {
    const { writes } = runToDone(
      inputs({ agents: [{ id: "ag_hello" }, { id: "ag_cfg" }], config: { agentId: "ag_cfg" } }),
    );
    expect(writes.agentId).toBe("ag_cfg");
  });

  it("falls back to the last-used agent when config sets none", () => {
    const { writes } = runToDone(
      inputs({ agents: [{ id: "ag_hello" }], lastAgentId: "ag_hello", config: {} }),
    );
    expect(writes.agentId).toBe("ag_hello");
  });

  it("seeds a config agent missing from the picker list verbatim — never the last-used", () => {
    // The create API accepts session-scoped agents the caller can read, so a
    // configured agent absent from the list is seeded as-is; if it's truly
    // unresolvable the composer surfaces an explicit unavailable state.
    // Substituting last-agent-id here silently rebound project sessions to
    // whatever agent the user last touched.
    const { writes } = runToDone(
      inputs({
        agents: [{ id: "ag_hello" }],
        lastAgentId: "ag_hello",
        config: { agentId: "ag_gone" },
      }),
    );
    expect(writes.agentId).toBe("ag_gone");
  });

  it("seeds a config agent before the picker list has loaded", () => {
    const step = projectPrefillStep(
      initialPrefillState("Alpha"),
      inputs({ agents: undefined, config: { agentId: "ag_cfg" } }),
    );
    expect(step).not.toBeNull();
    expect(step!.state.agentSeeded).toBe(true);
    expect(step!.writes.agentId).toBe("ag_cfg");
  });

  it("treats a blank config agent id as absent and falls back to the last-used agent", () => {
    // An empty or whitespace-only stored id is "not configured" — seeding it
    // verbatim would trip the composer's "agent unavailable" state for a
    // config that names no agent at all.
    for (const agentId of ["", "   "]) {
      const { writes } = runToDone(
        inputs({ agents: [{ id: "ag_hello" }], lastAgentId: "ag_hello", config: { agentId } }),
      );
      expect(writes.agentId).toBe("ag_hello");
    }
  });

  it("seeds no agent when neither config nor last-used is available", () => {
    const { state, writes } = runToDone(inputs({ lastAgentId: null, config: {} }));
    expect(writes.agentId).toBeUndefined();
    // Still marks the track done so the generic defaults can proceed.
    expect(state.agentSeeded).toBe(true);
  });

  it("waits for the agents list before settling the agent track", () => {
    const step = projectPrefillStep(
      initialPrefillState("Alpha"),
      inputs({ agents: undefined, config: { hostId: "host_1" } }),
    );
    // Host track can still settle, but the agent track stays open.
    expect(step).not.toBeNull();
    expect(step!.state.agentSeeded).toBe(false);
  });
});

describe("projectPrefill last start's agent", () => {
  it("waits for the pickable list before seeding any agent", () => {
    const step = projectPrefillStep(
      initialPrefillState("Alpha"),
      inputs({ agents: undefined, config: { agentId: "ag_cfg", launchAgentId: "ag_launch" } }),
    );
    expect(step!.state.agentSeeded).toBe(false);
    expect(step!.writes.agentId).toBeUndefined();
  });

  it.each([
    {
      label: "the last start's agent while the list has it",
      agents: ["ag_launch", "ag_cfg", "ag_hello"],
      config: { agentId: "ag_cfg", launchAgentId: "ag_launch" },
      seeded: "ag_launch",
    },
    {
      label: "the configured agent once the last start's is gone",
      agents: ["ag_cfg", "ag_hello"],
      config: { agentId: "ag_cfg", launchAgentId: "ag_gone" },
      seeded: "ag_cfg",
    },
    {
      label: "the last-used agent without a configured one",
      agents: ["ag_hello"],
      config: { launchAgentId: "ag_gone" },
      seeded: "ag_hello",
    },
  ])("seeds $label", ({ agents, config, seeded }) => {
    const { writes } = runToDone(
      inputs({ agents: agents.map((id) => ({ id })), lastAgentId: "ag_hello", config }),
    );
    expect(writes.agentId).toBe(seeded);
  });
});

describe("withProjectLaunchDefaults", () => {
  const stored = {
    hostId: "host_1",
    workspace: "/repos/alpha",
    agentId: "ag_claude",
    model: "opus",
    effort: "low",
    useWorktree: true,
  };

  it("keeps the stored defaults when the project has no last start", () => {
    expect(withProjectLaunchDefaults(stored, null)).toEqual(stored);
  });

  it.each<{ label: string; launch: ProjectLaunchDefaults; expected: ProjectPrefillConfig }>([
    {
      label: "keeps the stored model and effort for another agent's start",
      launch: { agentId: "ag_codex", harness: "codex-native", harnessOptions: { effort: "high" } },
      expected: { ...stored, launchAgentId: "ag_codex" },
    },
    {
      label: "drops them when the stored agent started with its own options",
      launch: { agentId: "ag_claude", harness: "claude-native", harnessOptions: { model: "" } },
      expected: { ...stored, launchAgentId: "ag_claude", model: undefined, effort: undefined },
    },
    {
      // A Smart Routing start records no harness options.
      label: "keeps them when the stored agent started without options",
      launch: { agentId: "ag_claude", pickedHarness: "auto-native" },
      expected: { ...stored, launchAgentId: "ag_claude" },
    },
  ])("$label", ({ launch, expected }) => {
    expect(withProjectLaunchDefaults(stored, launch)).toEqual(expected);
  });

  it("replaces the stored host together with its working directory", () => {
    expect(withProjectLaunchDefaults(stored, { hostId: SANDBOX_HOST_CHOICE })).toEqual({
      ...stored,
      hostId: SANDBOX_HOST_CHOICE,
      workspace: undefined,
    });
    expect(
      withProjectLaunchDefaults({}, { hostId: "host_2", workspace: "/repos/working-dir-b" }),
    ).toEqual({ hostId: "host_2", workspace: "/repos/working-dir-b" });
  });
});
