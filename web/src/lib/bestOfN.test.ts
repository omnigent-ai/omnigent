import { afterEach, describe, expect, it, vi } from "vitest";
import { testAgent } from "@/test/agentFixtures";
import type { Session } from "@/lib/types";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { createSession, postEvent, updateSession } from "@/lib/sessionsApi";
import {
  BEST_OF_N_ATTEMPTS_LABEL,
  BEST_OF_N_GROUP_LABEL,
  bestOfNAttemptIds,
  bestOfNBranchName,
  bestOfNRepository,
  launchBestOfN,
  otherBestOfNAttemptIds,
  verifiedOtherAttempts,
} from "./bestOfN";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  createSession: vi.fn(),
  postEvent: vi.fn(),
  updateSession: vi.fn(),
}));

const claude = testAgent("ag_claude", "claude-native-ui", {
  display_name: "Claude Code",
  harness: "claude-native",
});
const codex = testAgent("ag_codex", "codex-native-ui", {
  display_name: "Codex",
  harness: "codex-native",
});
const polly = testAgent("ag_polly", "polly", { display_name: "Polly", harness: "claude-sdk" });

afterEach(() => vi.clearAllMocks());

describe("best-of-N helpers", () => {
  it("branches attempts from the repository a worktree came from", () => {
    expect(bestOfNRepository("/Users/a/proj-worktrees/fix")).toBe("/Users/a/proj");
    expect(bestOfNRepository("/Users/a/proj/")).toBe("/Users/a/proj");
    expect(bestOfNRepository("/Users/a/proj")).toBe("/Users/a/proj");
  });

  it("names each attempt's branch after its run, position and agent", () => {
    expect(bestOfNBranchName("1a2b3c4d", 0, "Claude Code")).toBe(
      "best-of-n-1a2b3c4d-1-claude-code",
    );
    expect(bestOfNBranchName("1a2b3c4d", 3, "  ")).toBe("best-of-n-1a2b3c4d-4");
    expect(bestOfNBranchName("g", 1, "A very long agent display name!")).toBe(
      "best-of-n-g-2-a-very-long-agent-displa",
    );
  });

  it("reads the run's attempts only from a labelled attempt", () => {
    const labels = {
      [BEST_OF_N_GROUP_LABEL]: "g1",
      [BEST_OF_N_ATTEMPTS_LABEL]: "conv_a, conv_b,,conv_c,conv_a",
    };
    expect(bestOfNAttemptIds(labels)).toEqual(["conv_a", "conv_b", "conv_c"]);
    expect(otherBestOfNAttemptIds("conv_b", labels)).toEqual(["conv_a", "conv_c"]);
    // A session named by no list, or carrying a list without a group, is not an attempt.
    expect(otherBestOfNAttemptIds("conv_z", labels)).toEqual([]);
    expect(bestOfNAttemptIds({ [BEST_OF_N_ATTEMPTS_LABEL]: "conv_a,conv_b" })).toEqual([]);
    expect(bestOfNAttemptIds(undefined)).toEqual([]);
  });
});

describe("launchBestOfN", () => {
  it("creates one worktree attempt per agent, links them, and sends each the task", async () => {
    let next = 0;
    vi.mocked(createSession).mockImplementation(
      async () => ({ id: `conv_${++next}` }) as unknown as Session,
    );
    vi.mocked(postEvent).mockResolvedValue({ queued: true });
    vi.mocked(updateSession).mockResolvedValue({} as Session);

    const result = await launchBestOfN({
      sourceSessionId: "conv_source",
      hostId: "host_1",
      repository: "/repo",
      baseBranch: "  main ",
      prompt: "Fix the flaky test",
      agents: [claude, codex, claude],
    });

    expect(result.sessionIds).toEqual(["conv_1", "conv_2", "conv_3"]);
    expect(result.failures).toEqual([]);
    expect(result.groupId).toMatch(/^[0-9a-f]{8}$/);
    const calls = vi.mocked(createSession).mock.calls;
    expect(calls.map(([agentId]) => agentId)).toEqual(["ag_claude", "ag_codex", "ag_claude"]);
    expect(calls[1][2]).toEqual({
      siblingSourceId: "conv_source",
      hostId: "host_1",
      workspace: "/repo",
      git: { branchName: `best-of-n-${result.groupId}-2-codex`, baseBranch: "main" },
      title: "Attempt 2 · Codex",
      labels: {
        "omnigent.ui": "terminal",
        "omnigent.wrapper": "codex-native-ui",
        [BEST_OF_N_GROUP_LABEL]: result.groupId,
      },
    });
    // Same agent twice still gets distinct branches.
    expect(calls[0][2]?.git?.branchName).not.toBe(calls[2][2]?.git?.branchName);
    for (const id of result.sessionIds) {
      expect(updateSession).toHaveBeenCalledWith(id, {
        labels: { [BEST_OF_N_ATTEMPTS_LABEL]: "conv_1,conv_2,conv_3" },
      });
      expect(postEvent).toHaveBeenCalledWith(id, {
        type: "message",
        data: {
          role: "user",
          content: [{ type: "input_text", text: "Fix the flaky test" }],
          stable_id: expect.stringMatching(/^[0-9a-f]{32}$/),
        },
      });
    }
  });

  it("keeps going when one attempt fails and reports each failure", async () => {
    vi.mocked(createSession).mockImplementation(async (agentId) => {
      if (agentId === "ag_codex") throw new Error("branch already exists");
      return { id: `conv_${agentId}` } as unknown as Session;
    });
    vi.mocked(postEvent).mockImplementation(async (id) => {
      if (id === "conv_ag_polly") throw new Error("runner offline");
      return { queued: true };
    });
    vi.mocked(updateSession).mockResolvedValue({} as Session);

    const result = await launchBestOfN({
      sourceSessionId: "conv_source",
      hostId: "host_1",
      repository: "/repo",
      prompt: "Task",
      agents: [claude, codex, polly],
    });

    expect(result.sessionIds).toEqual(["conv_ag_claude", "conv_ag_polly"]);
    expect(result.failures).toEqual([
      { agentName: "Codex", message: "branch already exists" },
      { agentName: "Polly", message: "The task wasn't sent: runner offline" },
    ]);
    // Only created attempts are linked, and a blank base branch uses the repository's HEAD.
    expect(updateSession).toHaveBeenCalledTimes(2);
    expect(updateSession).toHaveBeenCalledWith("conv_ag_polly", {
      labels: { [BEST_OF_N_ATTEMPTS_LABEL]: "conv_ag_claude,conv_ag_polly" },
    });
    expect(vi.mocked(createSession).mock.calls[2][2]?.git?.baseBranch).toBeUndefined();
    // A composed agent gets no native terminal labels.
    expect(vi.mocked(createSession).mock.calls[2][2]?.labels).toEqual({
      [BEST_OF_N_GROUP_LABEL]: result.groupId,
    });
  });
});

describe("launchBestOfN ordering", () => {
  it("creates worktrees one at a time and links them only after every create settles", async () => {
    let inFlight = 0;
    let maxInFlight = 0;
    let next = 0;
    vi.mocked(createSession).mockImplementation(async () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise((resolve) => {
        setTimeout(resolve, 1);
      });
      inFlight -= 1;
      return { id: `conv_${++next}` } as unknown as Session;
    });
    vi.mocked(postEvent).mockResolvedValue({ queued: true });
    vi.mocked(updateSession).mockImplementation(async () => {
      expect(createSession).toHaveBeenCalledTimes(3);
      return {} as Session;
    });

    await launchBestOfN({
      sourceSessionId: "conv_source",
      hostId: "host_1",
      repository: "/repo",
      prompt: "Task",
      agents: [claude, codex, polly],
    });

    expect(maxInFlight).toBe(1);
    expect(updateSession).toHaveBeenCalledTimes(3);
  });
});

describe("verifiedOtherAttempts", () => {
  const run = (attempts: string, group = "g") => ({
    [BEST_OF_N_GROUP_LABEL]: group,
    [BEST_OF_N_ATTEMPTS_LABEL]: attempts,
  });
  const session = (id: string, overrides: Partial<Session> = {}) =>
    ({
      id,
      title: `Title ${id}`,
      labels: run("conv_a,conv_b,conv_c,conv_d,conv_e,conv_f"),
      permissionLevel: null,
      archived: false,
      ...overrides,
    }) as Session;

  it("keeps only owned, active members whose own labels name the same run", async () => {
    const fetchSession = vi.fn(async (id: string) => {
      if (id === "conv_c") return session(id, { labels: run("conv_a,conv_c", "other") });
      if (id === "conv_d") return session(id, { labels: run("conv_d,conv_e") });
      if (id === "conv_e") return session(id, { permissionLevel: 2 });
      if (id === "conv_f") return session(id, { archived: true });
      if (id === "conv_g") throw new Error("not found");
      return session(id, { title: null });
    });

    const verified = await verifiedOtherAttempts(
      "conv_a",
      run("conv_a,conv_b,conv_c,conv_d,conv_e,conv_f,conv_g"),
      fetchSession,
    );

    expect(verified).toEqual([{ id: "conv_b", title: "conv_b" }]);
  });

  it("checks nothing for a session that isn't an attempt", async () => {
    const fetchSession = vi.fn();
    expect(await verifiedOtherAttempts("conv_a", {}, fetchSession)).toEqual([]);
    expect(await verifiedOtherAttempts("conv_z", run("conv_a,conv_b"), fetchSession)).toEqual([]);
    expect(fetchSession).not.toHaveBeenCalled();
  });
});
