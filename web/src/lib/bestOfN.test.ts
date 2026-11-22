import { afterEach, describe, expect, it, vi } from "vitest";
import { testAgent } from "@/test/agentFixtures";
import type { Session } from "@/lib/types";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { ApiError, createSession, postEvent, updateSession } from "@/lib/sessionsApi";
import {
  BEST_OF_N_ATTEMPTS_LABEL,
  BEST_OF_N_GROUP_LABEL,
  bestOfNAttemptIds,
  bestOfNBranchName,
  bestOfNRepository,
  keepableRun,
  launchBestOfN,
  otherBestOfNAttemptIds,
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
      attempts: [
        { agent: claude },
        {
          agent: codex,
          modelOverride: "gpt-5",
          labels: { team: "infra", [BEST_OF_N_GROUP_LABEL]: "forged" },
        },
        { agent: claude },
      ],
    });

    expect(result.sessionIds).toEqual(["conv_1", "conv_2", "conv_3"]);
    expect(result.failures).toEqual([]);
    expect(result.groupId).toMatch(/^[0-9a-f]{8}$/);
    const calls = vi.mocked(createSession).mock.calls;
    expect(calls.map(([agentId]) => agentId)).toEqual(["ag_claude", "ag_codex", "ag_claude"]);
    // An attempt's own launch options ride along; its labels can't rename the run.
    expect(calls[1][2]).toEqual({
      modelOverride: "gpt-5",
      siblingSourceId: "conv_source",
      hostId: "host_1",
      workspace: "/repo",
      git: { branchName: `best-of-n-${result.groupId}-2-codex`, baseBranch: "main" },
      title: "Attempt 2 · Codex",
      labels: {
        "omnigent.ui": "terminal",
        team: "infra",
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

  it("keeps going when one attempt fails or is denied, and reports each failure", async () => {
    const cursor = testAgent("ag_cursor", "cursor-native-ui", {
      display_name: "Cursor",
      harness: "cursor-native",
    });
    vi.mocked(createSession).mockImplementation(async (agentId) => {
      if (agentId === "ag_codex") throw new Error("branch already exists");
      return { id: `conv_${agentId}` } as unknown as Session;
    });
    vi.mocked(postEvent).mockImplementation(async (id) => {
      if (id === "conv_ag_polly") throw new Error("runner offline");
      // A policy denial resolves: the request succeeded but no turn started.
      if (id === "conv_ag_cursor") return { queued: false, denied: true };
      return { queued: true };
    });
    vi.mocked(updateSession).mockResolvedValue({} as Session);

    const result = await launchBestOfN({
      sourceSessionId: "conv_source",
      hostId: "host_1",
      repository: "/repo",
      prompt: "Task",
      attempts: [claude, codex, polly, cursor].map((agent) => ({ agent })),
    });

    // A denied attempt stays in the run; nothing retries around the policy.
    expect(result.sessionIds).toEqual(["conv_ag_claude", "conv_ag_polly", "conv_ag_cursor"]);
    expect(result.failures).toEqual([
      { agentName: "Codex", message: "branch already exists" },
      { agentName: "Polly", message: "The task wasn't sent: runner offline" },
      { agentName: "Cursor", message: "A policy blocked the task, so no turn started." },
    ]);
    expect(vi.mocked(postEvent).mock.calls.map(([id]) => id)).toEqual([
      "conv_ag_claude",
      "conv_ag_polly",
      "conv_ag_cursor",
    ]);
    // Only created attempts are linked, and a blank base branch uses the repository's HEAD.
    expect(updateSession).toHaveBeenCalledTimes(3);
    for (const id of result.sessionIds)
      expect(updateSession).toHaveBeenCalledWith(id, {
        labels: { [BEST_OF_N_ATTEMPTS_LABEL]: "conv_ag_claude,conv_ag_polly,conv_ag_cursor" },
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
      attempts: [claude, codex, polly].map((agent) => ({ agent })),
    });

    expect(maxInFlight).toBe(1);
    expect(updateSession).toHaveBeenCalledTimes(3);
  });
});

describe("keepableRun", () => {
  const GROUP = "1a2b3c4d";
  const run = (attempts: string, group = GROUP) => ({
    [BEST_OF_N_GROUP_LABEL]: group,
    [BEST_OF_N_ATTEMPTS_LABEL]: attempts,
  });
  const session = (id: string, overrides: Partial<Session> = {}) =>
    ({
      id,
      title: `Title ${id}`,
      labels: run("conv_a,conv_b,conv_c,conv_d,conv_e,conv_f,conv_g,conv_h"),
      permissionLevel: null,
      archived: false,
      ...overrides,
    }) as Session;

  it("keeps the owned, active sessions that carry the kept attempt's run label", async () => {
    const fetchSession = vi.fn(async (id: string) => {
      // Listed by the kept attempt, but the server never put them in its run.
      if (id === "conv_c") return session(id, { labels: run("conv_a,conv_c", "0f0f0f0f") });
      if (id === "conv_d") return session(id, { labels: {} });
      if (id === "conv_e") return session(id, { permissionLevel: 2 });
      if (id === "conv_f") return session(id, { archived: true });
      if (id === "conv_g") throw new ApiError("Not found", 404, null);
      if (id === "conv_h") throw new ApiError("Forbidden", 403, null);
      // An attempt needn't list the kept one back: its run label is the proof.
      if (id === "conv_b") return session(id, { title: null, labels: { ...run(""), x: "1" } });
      return session(id);
    });

    expect(await keepableRun("conv_a", fetchSession)).toEqual({
      groupId: GROUP,
      keptArchived: false,
      others: [{ id: "conv_b", title: "conv_b" }],
    });
  });

  it("reads the run from the server, and reports a kept attempt that is archived", async () => {
    const fetchSession = vi.fn(async (id: string) =>
      session(id, { labels: run("conv_a,conv_b"), archived: id === "conv_a" }),
    );
    expect(await keepableRun("conv_a", fetchSession)).toEqual({
      groupId: GROUP,
      keptArchived: true,
      others: [{ id: "conv_b", title: "Title conv_b" }],
    });
  });

  it("rejects on a failed lookup rather than return part of the run", async () => {
    let failing = true;
    const fetchSession = vi.fn(async (id: string) => {
      if (id === "conv_c" && failing) throw new ApiError("Internal Server Error", 500, null);
      return session(id, { labels: run("conv_a,conv_b,conv_c") });
    });
    await expect(keepableRun("conv_a", fetchSession)).rejects.toThrow("Internal Server Error");
    failing = false;
    expect((await keepableRun("conv_a", fetchSession))?.others.map(({ id }) => id)).toEqual([
      "conv_b",
      "conv_c",
    ]);
    // A network failure on the kept attempt is no proof it left the run either.
    fetchSession.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(keepableRun("conv_a", fetchSession)).rejects.toThrow("Failed to fetch");
  });

  it("finds no run for a session that isn't a readable attempt", async () => {
    const unlabelled = vi.fn(async (id: string) => session(id, { labels: {} }));
    expect(await keepableRun("conv_a", unlabelled)).toBeNull();
    expect(unlabelled).toHaveBeenCalledTimes(1);
    const missing = vi.fn(async () => {
      throw new ApiError("Not found", 404, null);
    });
    expect(await keepableRun("conv_a", missing)).toBeNull();
    // An attempt no list names has nothing to look up.
    const unlisted = vi.fn(async (id: string) => session(id, { labels: run("conv_b,conv_c") }));
    expect((await keepableRun("conv_a", unlisted))?.others).toEqual([]);
    expect(unlisted).toHaveBeenCalledTimes(1);
  });
});
