import { beforeEach, describe, expect, it } from "vitest";
import { clearSessionSnapshotStatuses, mergeSessionSnapshot } from "./sessionSnapshot";
import type { Session } from "./types";

const owner = {
  runner_id: "runner_B",
  binding_epoch: 2,
  revision: 5,
  observation_id: "current",
  status: "waiting",
  response_id: "turn_B",
  error_key: null,
};

function snapshot(overrides: Partial<Session> = {}): Session {
  return {
    id: "session",
    agentId: "agent",
    agentName: null,
    status: "waiting",
    createdAt: 0,
    title: null,
    items: [],
    permissionLevel: null,
    parentSessionId: null,
    subAgentName: null,
    kind: "default",
    sessionObservation: owner,
    ...overrides,
  };
}

beforeEach(clearSessionSnapshotStatuses);

describe("owned snapshot state", () => {
  it("retains the current owner's pending and startup state when an old snapshot settles", () => {
    const current = snapshot({
      terminalPending: false,
      sandboxStatus: null,
      mcpStartup: null,
      pendingElicitations: [{ elicitation_id: "current_approval" }],
      pendingInputs: [
        { pendingId: "current_input", content: [{ type: "input_text", text: "Current input" }] },
      ],
    });
    const merged = mergeSessionSnapshot(
      current,
      snapshot({
        sessionObservation: { ...owner, runner_id: "runner_A", binding_epoch: 1, revision: 4 },
        title: "Independent title",
        terminalPending: true,
        sandboxStatus: { stage: "provisioning", error: null },
        mcpStartup: { old: { status: "starting", error: null } },
        pendingElicitations: [{ elicitation_id: "old_approval" }],
        pendingInputs: [
          { pendingId: "old_input", content: [{ type: "input_text", text: "Old input" }] },
        ],
      }),
    );
    expect(merged.title).toBe("Independent title");
    expect(merged.sessionObservation).toEqual(owner);
    expect(merged.terminalPending).toBe(false);
    expect(merged.sandboxStatus).toBeNull();
    expect(merged.mcpStartup).toBeNull();
    expect(merged.pendingElicitations).toEqual(current.pendingElicitations);
    expect(merged.pendingInputs).toEqual(current.pendingInputs);
  });

  it("accepts genuine current pending state instead of clearing it on a duplicate owner", () => {
    const incoming = snapshot({
      terminalPending: true,
      mcpStartup: { current: { status: "starting", error: null } },
      pendingElicitations: [{ elicitation_id: "current_approval" }],
      pendingInputs: [{ pendingId: "current_input", content: [] }],
    });
    expect(mergeSessionSnapshot(snapshot(), incoming)).toEqual(incoming);
  });
});
