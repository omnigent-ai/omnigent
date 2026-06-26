import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CodexGoalDialog, parseCodexGoalBudget } from "./CodexGoalDialog";
import type { CodexGoal } from "@/lib/codexGoalApi";
import {
  clearCodexGoal,
  getCodexGoal,
  setCodexGoal,
  updateCodexGoalStatus,
} from "@/lib/codexGoalApi";

vi.mock("@/lib/codexGoalApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/codexGoalApi")>();
  return {
    ...actual,
    clearCodexGoal: vi.fn(),
    getCodexGoal: vi.fn(),
    setCodexGoal: vi.fn(),
    updateCodexGoalStatus: vi.fn(),
  };
});

const mockGetCodexGoal = vi.mocked(getCodexGoal);
const mockSetCodexGoal = vi.mocked(setCodexGoal);
const mockClearCodexGoal = vi.mocked(clearCodexGoal);
const mockUpdateCodexGoalStatus = vi.mocked(updateCodexGoalStatus);

const ACTIVE_GOAL: CodexGoal = {
  threadId: "thread-1",
  objective: "Ship goal mode",
  status: "active",
  tokenBudget: 40000,
  tokensUsed: 1200,
  timeUsedSeconds: 125,
  createdAt: 1,
  updatedAt: 2,
};

function renderDialog({
  goal = ACTIVE_GOAL,
  readOnly = false,
  conversationId = "conv_codex",
  onGoalChange = vi.fn(),
}: {
  goal?: CodexGoal | null;
  readOnly?: boolean;
  conversationId?: string | null;
  onGoalChange?: (goal: CodexGoal | null) => void;
} = {}) {
  const onOpenChange = vi.fn();
  render(
    <CodexGoalDialog
      open
      onOpenChange={onOpenChange}
      conversationId={conversationId}
      readOnly={readOnly}
      goal={goal}
      onGoalChange={onGoalChange}
    />,
  );
  return { onGoalChange, onOpenChange };
}

describe("CodexGoalDialog", () => {
  beforeEach(() => {
    mockGetCodexGoal.mockResolvedValue({ goal: ACTIVE_GOAL });
    mockSetCodexGoal.mockResolvedValue({ goal: ACTIVE_GOAL });
    mockClearCodexGoal.mockResolvedValue({ cleared: true });
    mockUpdateCodexGoalStatus.mockImplementation(async (_sessionId, status) => ({
      goal: { ...ACTIVE_GOAL, status },
    }));
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("loads the current goal when opened", async () => {
    const { onGoalChange } = renderDialog({ goal: null });

    expect(screen.getByText("Loading goal")).toBeInTheDocument();
    await waitFor(() => expect(mockGetCodexGoal).toHaveBeenCalledWith("conv_codex"));
    expect(onGoalChange).toHaveBeenCalledWith(ACTIVE_GOAL);
  });

  it("displays the current goal summary", async () => {
    renderDialog();

    await waitFor(() => expect(mockGetCodexGoal).toHaveBeenCalledWith("conv_codex"));
    expect(screen.getByTestId("codex-goal-current")).toHaveTextContent("Ship goal mode");
    expect(screen.getByTestId("codex-goal-current")).toHaveTextContent(
      "1,200 / 40,000 tokens / 2 min",
    );
  });

  it("saves a trimmed objective, token budget, and selected status", async () => {
    const updatedGoal = { ...ACTIVE_GOAL, objective: "Finish tests", status: "paused" };
    mockSetCodexGoal.mockResolvedValueOnce({ goal: updatedGoal });
    const { onGoalChange } = renderDialog({ goal: null });
    await waitFor(() => expect(mockGetCodexGoal).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("codex-goal-objective"), {
      target: { value: "  Finish tests  " },
    });
    fireEvent.change(screen.getByTestId("codex-goal-token-budget"), {
      target: { value: "123" },
    });
    fireEvent.click(screen.getByTestId("codex-goal-mode-paused"));
    fireEvent.click(screen.getByTestId("codex-goal-save"));

    await waitFor(() =>
      expect(mockSetCodexGoal).toHaveBeenCalledWith("conv_codex", {
        objective: "Finish tests",
        tokenBudget: 123,
        status: "paused",
      }),
    );
    expect(onGoalChange).toHaveBeenLastCalledWith(updatedGoal);
  });

  it("preserves Codex-owned statuses when keep-current mode is selected", async () => {
    const blockedGoal = { ...ACTIVE_GOAL, status: "blocked" };
    mockGetCodexGoal.mockResolvedValueOnce({ goal: blockedGoal });
    renderDialog({ goal: blockedGoal });
    await waitFor(() => expect(screen.getByTestId("codex-goal-mode-keep")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("codex-goal-save"));

    await waitFor(() =>
      expect(mockSetCodexGoal).toHaveBeenCalledWith(
        "conv_codex",
        expect.objectContaining({ status: undefined }),
      ),
    );
  });

  it("re-submits the goal even when nothing changed", async () => {
    // #1289 follow-up: "Update goal" must always re-apply the goal. Re-submitting
    // the same objective is valid and must still hit the API (no silent no-op).
    renderDialog({ goal: ACTIVE_GOAL });
    await waitFor(() => expect(screen.getByTestId("codex-goal-current")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("codex-goal-save"));

    await waitFor(() =>
      expect(mockSetCodexGoal).toHaveBeenCalledWith(
        "conv_codex",
        expect.objectContaining({ objective: ACTIVE_GOAL.objective }),
      ),
    );
  });

  it("keeps the objective editable while a save is in flight", async () => {
    // #1289: the flicker was the whole editor dimming on each save. The editor
    // must stay enabled during an in-flight save (only the initial load disables it).
    let resolveSave: (value: { goal: CodexGoal }) => void = () => {};
    mockSetCodexGoal.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    renderDialog({ goal: ACTIVE_GOAL });
    await waitFor(() => expect(screen.getByTestId("codex-goal-current")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("codex-goal-save"));

    await waitFor(() => expect(mockSetCodexGoal).toHaveBeenCalled());
    expect(screen.getByTestId("codex-goal-objective")).not.toBeDisabled();

    resolveSave({ goal: ACTIVE_GOAL });
    await waitFor(() => expect(screen.getByTestId("codex-goal-save")).not.toBeDisabled());
  });

  it("shows validation errors without calling the API", async () => {
    renderDialog({ goal: null });
    await waitFor(() => expect(mockGetCodexGoal).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("codex-goal-objective"), { target: { value: "" } });
    fireEvent.click(screen.getByTestId("codex-goal-save"));
    expect(await screen.findByText("Goal objective cannot be empty.")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("codex-goal-objective"), {
      target: { value: "Do the work" },
    });
    fireEvent.change(screen.getByTestId("codex-goal-token-budget"), { target: { value: "1.5" } });
    fireEvent.click(screen.getByTestId("codex-goal-save"));
    expect(
      await screen.findByText("Token budget must be a positive whole number."),
    ).toBeInTheDocument();
    expect(mockSetCodexGoal).not.toHaveBeenCalled();
  });

  it("clears and pauses or resumes goals", async () => {
    const { onGoalChange } = renderDialog();
    await waitFor(() => expect(mockGetCodexGoal).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId("codex-goal-pause"));
    await waitFor(() =>
      expect(mockUpdateCodexGoalStatus).toHaveBeenCalledWith("conv_codex", "paused"),
    );

    cleanup();
    renderDialog({ goal: { ...ACTIVE_GOAL, status: "blocked" }, onGoalChange });
    await waitFor(() => expect(mockGetCodexGoal).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByTestId("codex-goal-resume"));
    await waitFor(() =>
      expect(mockUpdateCodexGoalStatus).toHaveBeenLastCalledWith("conv_codex", "active"),
    );

    fireEvent.click(screen.getByTestId("codex-goal-clear"));
    await waitFor(() => expect(mockClearCodexGoal).toHaveBeenCalledWith("conv_codex"));
    expect(onGoalChange).toHaveBeenLastCalledWith(null);
  });

  it("disables write actions in read-only mode", async () => {
    renderDialog({ readOnly: true });
    await waitFor(() => expect(mockGetCodexGoal).toHaveBeenCalled());

    expect(screen.getByTestId("codex-goal-objective")).toBeDisabled();
    expect(screen.getByTestId("codex-goal-token-budget")).toBeDisabled();
    expect(screen.getByTestId("codex-goal-save")).toBeDisabled();
    expect(screen.getByTestId("codex-goal-clear")).toBeDisabled();
    expect(screen.getByTestId("codex-goal-pause")).toBeDisabled();
  });

  it("surfaces API errors", async () => {
    mockGetCodexGoal.mockRejectedValueOnce(new Error("runner is asleep"));
    renderDialog({ goal: null });

    expect(await screen.findByText("Could not read goal: runner is asleep")).toBeInTheDocument();
  });
});

describe("Codex goal budget parsing", () => {
  it("returns null for blank budgets and parses positive safe integers", () => {
    expect(parseCodexGoalBudget(" ")).toBeNull();
    expect(parseCodexGoalBudget("40000")).toBe(40000);
  });

  it("rejects non-positive, fractional, and unsafe budgets", () => {
    expect(() => parseCodexGoalBudget("0")).toThrow(/positive whole number/);
    expect(() => parseCodexGoalBudget("1.5")).toThrow(/positive whole number/);
    expect(() => parseCodexGoalBudget("9007199254740992")).toThrow(/positive whole number/);
  });
});
