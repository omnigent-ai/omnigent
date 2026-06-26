import { afterEach, describe, expect, it, vi } from "vitest";
import type { FlowGraph } from "./flowToText";

// Mock the API layer so the store is exercised in isolation (no network).
vi.mock("./jobsApi", () => {
  return {
    apiCreateJob: vi.fn(),
    apiUpdateJob: vi.fn(),
    apiDeleteJob: vi.fn(),
    apiGetJob: vi.fn(),
    apiListJobs: vi.fn(),
    apiRunJob: vi.fn(),
  };
});

import * as api from "./jobsApi";
import { createJob, runJob, updateJob } from "./jobsStore";

function graphWithNode(): FlowGraph {
  return {
    nodes: [{ id: "n1", type: "start", label: "Start", x: 0, y: 0 }],
    edges: [],
    loops: [],
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("jobsStore", () => {
  it("createJob renders the narrative from the graph and posts it", async () => {
    const mockCreate = vi.mocked(api.apiCreateJob);
    mockCreate.mockResolvedValue({
      id: "job_1",
      name: "Flow",
      createdAt: 1000,
      updatedAt: 1000,
      graph: graphWithNode(),
      narrative: "x",
      agentId: null,
    });

    await createJob("Flow", graphWithNode());

    expect(mockCreate).toHaveBeenCalledTimes(1);
    const arg = mockCreate.mock.calls[0][0];
    expect(arg.name).toBe("Flow");
    expect(arg.graph).toEqual(graphWithNode());
    // The narrative is generated client-side from the graph, not blank.
    expect(typeof arg.narrative).toBe("string");
    expect(arg.narrative!.length).toBeGreaterThan(0);
  });

  it("createJob defaults a blank name to 'Untitled flow'", async () => {
    const mockCreate = vi.mocked(api.apiCreateJob);
    mockCreate.mockResolvedValue({
      id: "job_2",
      name: "Untitled flow",
      createdAt: 1,
      updatedAt: 1,
      graph: graphWithNode(),
      narrative: "x",
      agentId: null,
    });

    await createJob("   ");
    expect(mockCreate.mock.calls[0][0].name).toBe("Untitled flow");
  });

  it("updateJob re-renders the narrative when the graph changes", async () => {
    const mockUpdate = vi.mocked(api.apiUpdateJob);
    mockUpdate.mockResolvedValue({
      id: "job_1",
      name: "Flow",
      createdAt: 1,
      updatedAt: 2,
      graph: graphWithNode(),
      narrative: "x",
      agentId: null,
    });

    await updateJob("job_1", { graph: graphWithNode() });

    const [id, input] = mockUpdate.mock.calls[0];
    expect(id).toBe("job_1");
    expect(input.graph).toEqual(graphWithNode());
    expect(input.narrative).toBeDefined();
    expect(input.narrative!.length).toBeGreaterThan(0);
  });

  it("updateJob without a graph change does not send a narrative", async () => {
    const mockUpdate = vi.mocked(api.apiUpdateJob);
    mockUpdate.mockResolvedValue({
      id: "job_1",
      name: "Renamed",
      createdAt: 1,
      updatedAt: 2,
      graph: graphWithNode(),
      narrative: "x",
      agentId: null,
    });

    await updateJob("job_1", { name: "Renamed" });

    const input = mockUpdate.mock.calls[0][1];
    expect(input.name).toBe("Renamed");
    expect(input.narrative).toBeUndefined();
    expect(input.graph).toBeUndefined();
  });

  it("runJob delegates to the API and returns the run", async () => {
    const mockRun = vi.mocked(api.apiRunJob);
    mockRun.mockResolvedValue({
      id: "run_1",
      jobId: "job_1",
      sessionId: "conv_1",
      status: "running",
      startedAt: 1000,
      completedAt: null,
      error: null,
    });

    const run = await runJob("job_1");
    expect(mockRun).toHaveBeenCalledWith("job_1");
    expect(run.sessionId).toBe("conv_1");
  });
});
