import { beforeEach, describe, expect, it, vi } from "vitest";
import { clearCodexGoal, getCodexGoal, setCodexGoal, updateCodexGoalStatus } from "./codexGoalApi";
import { clearGoal, getGoal, setGoal, updateGoalStatus } from "./goalApi";

vi.mock("./codexGoalApi", () => ({
  clearCodexGoal: vi.fn(),
  getCodexGoal: vi.fn(),
  setCodexGoal: vi.fn(),
  updateCodexGoalStatus: vi.fn(),
}));

const mockClearCodexGoal = vi.mocked(clearCodexGoal);
const mockGetCodexGoal = vi.mocked(getCodexGoal);
const mockSetCodexGoal = vi.mocked(setCodexGoal);
const mockUpdateCodexGoalStatus = vi.mocked(updateCodexGoalStatus);

const CODEX_GOAL = {
  threadId: "thread-1",
  objective: "Ship goal mode",
  status: "active",
  tokenBudget: 40_000,
  tokensUsed: 1_200,
  timeUsedSeconds: 125,
  createdAt: null,
  updatedAt: null,
};

describe("goal API facade", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetCodexGoal.mockResolvedValue({ goal: CODEX_GOAL });
    mockSetCodexGoal.mockResolvedValue({ goal: CODEX_GOAL });
    mockUpdateCodexGoalStatus.mockResolvedValue({ goal: CODEX_GOAL });
    mockClearCodexGoal.mockResolvedValue({ cleared: true });
  });

  it("delegates reads and mutations to the Codex backend", async () => {
    await expect(getGoal("conv")).resolves.toEqual({ goal: CODEX_GOAL });
    await expect(
      setGoal("conv", { objective: "Ship goal mode", tokenBudget: 40_000, status: "active" }),
    ).resolves.toEqual({ goal: CODEX_GOAL });
    await expect(updateGoalStatus("conv", "paused")).resolves.toEqual({ goal: CODEX_GOAL });
    await expect(clearGoal("conv")).resolves.toEqual({ cleared: true });

    expect(mockGetCodexGoal).toHaveBeenCalledWith("conv");
    expect(mockSetCodexGoal).toHaveBeenCalledWith("conv", {
      objective: "Ship goal mode",
      tokenBudget: 40_000,
      status: "active",
    });
    expect(mockUpdateCodexGoalStatus).toHaveBeenCalledWith("conv", "paused");
    expect(mockClearCodexGoal).toHaveBeenCalledWith("conv");
  });
});
