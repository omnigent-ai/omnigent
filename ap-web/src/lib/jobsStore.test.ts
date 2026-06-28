import { afterEach, describe, expect, it, vi } from "vitest";
import { newTree, attach } from "./flowTree";

// Mock the API layer so the store is exercised in isolation (no network).
vi.mock("./jobsApi", () => {
  return {
    apiCreateJob: vi.fn(),
    apiUpdateJob: vi.fn(),
    apiDeleteJob: vi.fn(),
    apiGetJob: vi.fn(),
    apiListJobs: vi.fn(),
    apiListRuns: vi.fn(),
    apiRunJob: vi.fn(),
  };
});

import * as api from "./jobsApi";
import { createJob, runJob, updateJob } from "./jobsStore";

/** A tree with a real step after Start, so the narrative is non-trivial. */
function treeWithStep() {
  const t = newTree();
  return attach(t, t.id, "next", "process");
}

function apiJob(overrides: Partial<api.Job> = {}): api.Job {
  return {
    id: "job_1",
    name: "Flow",
    createdAt: 1000,
    updatedAt: 1000,
    graph: treeWithStep(),
    narrative: "x",
    agentId: null,
    scheduleConfig: null,
    hostId: null,
    ...overrides,
  };
}

/** A backend run fixture with the required `trigger` field. */
function apiRun(overrides: Partial<api.Run> = {}): api.Run {
  return {
    id: "run_1",
    jobId: "job_1",
    sessionId: "conv_1",
    status: "running",
    startedAt: 1000,
    completedAt: null,
    error: null,
    trigger: "adhoc",
    ...overrides,
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("jobsStore", () => {
  it("createJob persists the tree as the graph and renders a narrative", async () => {
    const mockCreate = vi.mocked(api.apiCreateJob);
    mockCreate.mockResolvedValue(apiJob());

    const tree = treeWithStep();
    await createJob("Flow", tree);

    expect(mockCreate).toHaveBeenCalledTimes(1);
    const arg = mockCreate.mock.calls[0][0];
    expect(arg.name).toBe("Flow");
    expect(arg.graph).toBe(tree); // tree stored verbatim as opaque graph JSON
    // Narrative is generated client-side from the tree, not blank.
    expect(typeof arg.narrative).toBe("string");
    expect(arg.narrative!.length).toBeGreaterThan(0);
  });

  it("createJob defaults a blank name to 'Untitled flow'", async () => {
    const mockCreate = vi.mocked(api.apiCreateJob);
    mockCreate.mockResolvedValue(apiJob({ name: "Untitled flow" }));

    await createJob("   ");
    expect(mockCreate.mock.calls[0][0].name).toBe("Untitled flow");
  });

  it("updateJob re-renders the narrative when the tree changes", async () => {
    vi.mocked(api.apiUpdateJob).mockResolvedValue(apiJob());
    vi.mocked(api.apiGetJob).mockResolvedValue(apiJob());
    vi.mocked(api.apiListRuns).mockResolvedValue([]);

    await updateJob("job_1", { tree: treeWithStep() });

    const [id, input] = vi.mocked(api.apiUpdateJob).mock.calls[0];
    expect(id).toBe("job_1");
    expect(input.graph).toBeDefined();
    expect(input.narrative).toBeDefined();
    expect(input.narrative!.length).toBeGreaterThan(0);
  });

  it("updateJob without a tree change does not send a narrative", async () => {
    vi.mocked(api.apiUpdateJob).mockResolvedValue(apiJob({ name: "Renamed" }));
    vi.mocked(api.apiGetJob).mockResolvedValue(apiJob({ name: "Renamed" }));
    vi.mocked(api.apiListRuns).mockResolvedValue([]);

    await updateJob("job_1", { name: "Renamed" });

    const input = vi.mocked(api.apiUpdateJob).mock.calls[0][1];
    expect(input.name).toBe("Renamed");
    expect(input.narrative).toBeUndefined();
    expect(input.graph).toBeUndefined();
  });

  it("runJob triggers the backend run and returns the latest run with its session", async () => {
    vi.mocked(api.apiRunJob).mockResolvedValue(apiRun());
    vi.mocked(api.apiGetJob).mockResolvedValue(apiJob());
    vi.mocked(api.apiListRuns).mockResolvedValue([apiRun()]);

    const run = await runJob("job_1");
    expect(api.apiRunJob).toHaveBeenCalledWith("job_1");
    expect(run?.sessionId).toBe("conv_1");
    expect(run?.number).toBe(1);
  });
});
