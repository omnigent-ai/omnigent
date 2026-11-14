import type * as GoalApiModule from "@/lib/goalApi";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GoalDialog, parseGoalBudget } from "./GoalDialog";
import type { Goal } from "@/lib/goalApi";
import { clearGoal, getGoal, setGoal, updateGoalStatus } from "@/lib/goalApi";

vi.mock("@/lib/goalApi", async (importOriginal) => {
  const actual = await importOriginal<typeof GoalApiModule>();
  return {
    ...actual,
    clearGoal: vi.fn(),
    getGoal: vi.fn(),
    setGoal: vi.fn(),
    updateGoalStatus: vi.fn(),
  };
});

const mockGetGoal = vi.mocked(getGoal);
const mockSetGoal = vi.mocked(setGoal);
const mockClearGoal = vi.mocked(clearGoal);
const mockUpdateGoalStatus = vi.mocked(updateGoalStatus);

const ACTIVE_GOAL: Goal = {
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
  goal?: Goal | null;
  readOnly?: boolean;
  conversationId?: string | null;
  onGoalChange?: (goal: Goal | null) => void;
} = {}) {
  const onOpenChange = vi.fn();
  render(
    <GoalDialog
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

describe("GoalDialog", () => {
  beforeEach(() => {
    mockGetGoal.mockResolvedValue({ goal: ACTIVE_GOAL });
    mockSetGoal.mockResolvedValue({ goal: ACTIVE_GOAL });
    mockClearGoal.mockResolvedValue({ cleared: true });
    mockUpdateGoalStatus.mockImplementation(async (_sessionId, status) => ({
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
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledWith("conv_codex"));
    expect(onGoalChange).toHaveBeenCalledWith(ACTIVE_GOAL);
  });

  it("displays the current goal summary", async () => {
    renderDialog();

    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledWith("conv_codex"));
    expect(screen.getByTestId("goal-current")).toHaveTextContent("Ship goal mode");
    expect(screen.getByTestId("goal-current")).toHaveTextContent("1,200 / 40,000 tokens / 2 min");
  });

  it("lets an unbreakable objective wrap inside the summary card", async () => {
    renderDialog();

    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledWith("conv_codex"));
    const objective = within(screen.getByTestId("goal-current")).getByText("Ship goal mode");
    expect(objective).toHaveClass("wrap-anywhere");
  });

  it("saves a trimmed objective, token budget, and selected status", async () => {
    const updatedGoal = { ...ACTIVE_GOAL, objective: "Finish tests", status: "paused" };
    mockSetGoal.mockResolvedValueOnce({ goal: updatedGoal });
    const { onGoalChange } = renderDialog({ goal: null });
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("goal-objective"), {
      target: { value: "  Finish tests  " },
    });
    fireEvent.change(screen.getByTestId("goal-token-budget"), {
      target: { value: "123" },
    });
    fireEvent.click(screen.getByTestId("goal-mode-paused"));
    fireEvent.click(screen.getByTestId("goal-save"));

    await waitFor(() =>
      expect(mockSetGoal).toHaveBeenCalledWith("conv_codex", {
        objective: "Finish tests",
        tokenBudget: 123,
        status: "paused",
      }),
    );
    expect(onGoalChange).toHaveBeenLastCalledWith(updatedGoal);
  });

  it("keeps unsaved edits when background goal snapshots change", async () => {
    const onGoalChange = vi.fn();
    const onOpenChange = vi.fn();
    const props = {
      open: true,
      conversationId: "conv_codex",
      readOnly: false,
      goal: ACTIVE_GOAL,
      onGoalChange,
      onOpenChange,
    };
    const { rerender } = render(<GoalDialog {...props} />);
    await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
    fireEvent.change(screen.getByTestId("goal-objective"), {
      target: { value: "My unsaved objective" },
    });
    fireEvent.change(screen.getByTestId("goal-token-budget"), {
      target: { value: "12345" },
    });
    fireEvent.click(screen.getByTestId("goal-mode-paused"));

    for (const goal of [
      { ...ACTIVE_GOAL },
      { ...ACTIVE_GOAL, status: "complete", tokensUsed: 2000 },
    ]) {
      rerender(<GoalDialog {...props} goal={goal} />);
      expect(screen.getByTestId("goal-objective")).toHaveValue("My unsaved objective");
      expect(screen.getByTestId("goal-token-budget")).toHaveValue(12345);
      expect(screen.getByTestId("goal-mode-paused")).toHaveAttribute("aria-checked", "true");
    }
    expect(screen.getByTestId("goal-current")).toHaveTextContent("complete");
    expect(screen.getByTestId("goal-current")).toHaveTextContent("2,000");
  });

  it("preserves Codex-owned statuses when keep-current mode is selected", async () => {
    const blockedGoal = { ...ACTIVE_GOAL, status: "blocked" };
    mockGetGoal.mockResolvedValueOnce({ goal: blockedGoal });
    renderDialog({ goal: blockedGoal });
    await waitFor(() => expect(screen.getByTestId("goal-mode-keep")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("goal-save"));

    await waitFor(() =>
      expect(mockSetGoal).toHaveBeenCalledWith(
        "conv_codex",
        expect.objectContaining({ status: undefined }),
      ),
    );
  });

  it.each(["complete", "blocked", "usageLimited"])(
    "does not overwrite an untouched %s mode when saving an objective edit",
    async (status) => {
      const props = {
        open: true,
        conversationId: "conv_codex",
        readOnly: false,
        goal: ACTIVE_GOAL,
        onGoalChange: vi.fn(),
        onOpenChange: vi.fn(),
      };
      const { rerender } = render(<GoalDialog {...props} />);
      await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
      fireEvent.change(screen.getByTestId("goal-objective"), {
        target: { value: "My edited objective" },
      });

      rerender(<GoalDialog {...props} goal={{ ...ACTIVE_GOAL, status, tokenBudget: 50000 }} />);
      expect(screen.getByTestId("goal-mode-keep")).toHaveAttribute("aria-checked", "true");
      expect(screen.getByTestId("goal-token-budget")).toHaveValue(50000);
      fireEvent.click(screen.getByTestId("goal-save"));

      await waitFor(() =>
        expect(mockSetGoal).toHaveBeenCalledWith("conv_codex", {
          objective: "My edited objective",
          tokenBudget: 50000,
          status: undefined,
        }),
      );
    },
  );

  it.each(["reopen", "session switch"])(
    "discards old drafts on %s even when the new read fails",
    async (boundary) => {
      const props = {
        open: true,
        conversationId: "conv_codex",
        readOnly: false,
        goal: ACTIVE_GOAL,
        onGoalChange: vi.fn(),
        onOpenChange: vi.fn(),
      };
      const { rerender } = render(<GoalDialog {...props} />);
      await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
      fireEvent.change(screen.getByTestId("goal-objective"), {
        target: { value: "Cancelled objective" },
      });
      fireEvent.change(screen.getByTestId("goal-token-budget"), { target: { value: "123" } });
      fireEvent.click(screen.getByTestId("goal-mode-paused"));
      mockGetGoal.mockRejectedValueOnce(new Error("runner unavailable"));

      const nextGoal = {
        ...ACTIVE_GOAL,
        objective: boundary === "reopen" ? "Updated while closed" : "Another session",
        tokenBudget: 777,
      };
      const nextId = boundary === "reopen" ? props.conversationId : "conv_other";
      if (boundary === "reopen") rerender(<GoalDialog {...props} open={false} />);
      rerender(<GoalDialog {...props} goal={nextGoal} conversationId={nextId} />);

      await screen.findByText("Could not read goal: runner unavailable");
      expect(screen.getByTestId("goal-objective")).toHaveValue(nextGoal.objective);
      expect(screen.getByTestId("goal-token-budget")).toHaveValue(nextGoal.tokenBudget);
      expect(screen.getByTestId("goal-mode-active")).toHaveAttribute("aria-checked", "true");
      fireEvent.click(screen.getByTestId("goal-save"));
      await waitFor(() =>
        expect(mockSetGoal).toHaveBeenCalledWith(nextId, {
          objective: nextGoal.objective,
          tokenBudget: nextGoal.tokenBudget,
          status: "active",
        }),
      );
    },
  );

  it("does not restore a deleted goal after reopening with a failed read", async () => {
    const props = {
      open: true,
      conversationId: "conv_codex",
      readOnly: false,
      goal: ACTIVE_GOAL,
      onGoalChange: vi.fn(),
      onOpenChange: vi.fn(),
    };
    const { rerender } = render(<GoalDialog {...props} />);
    await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
    rerender(<GoalDialog {...props} open={false} />);
    mockGetGoal.mockRejectedValueOnce(new Error("runner unavailable"));
    rerender(<GoalDialog {...props} goal={null} />);

    await screen.findByText("Could not read goal: runner unavailable");
    expect(screen.getByTestId("goal-objective")).toHaveValue("");
    expect(screen.getByTestId("goal-token-budget")).toHaveValue(null);
    fireEvent.click(screen.getByTestId("goal-save"));
    expect(screen.getByText("Goal objective cannot be empty.")).toBeInTheDocument();
    expect(mockSetGoal).not.toHaveBeenCalled();
  });

  it("ignores an opening read that finishes after switching sessions", async () => {
    let resolveOldRead!: (response: { goal: Goal | null }) => void;
    mockGetGoal.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOldRead = resolve;
      }),
    );
    const props = {
      open: true,
      conversationId: "conv_codex",
      readOnly: false,
      goal: ACTIVE_GOAL,
      onGoalChange: vi.fn(),
      onOpenChange: vi.fn(),
    };
    const { rerender } = render(<GoalDialog {...props} />);
    const nextGoal = { ...ACTIVE_GOAL, objective: "Current session", tokenBudget: 777 };
    mockGetGoal.mockResolvedValueOnce({ goal: nextGoal });
    rerender(<GoalDialog {...props} goal={nextGoal} conversationId="conv_other" />);
    await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());

    await act(async () => resolveOldRead({ goal: ACTIVE_GOAL }));

    expect(screen.getByTestId("goal-objective")).toHaveValue(nextGoal.objective);
    expect(screen.getByTestId("goal-token-budget")).toHaveValue(nextGoal.tokenBudget);
    expect(props.onGoalChange).toHaveBeenCalledTimes(1);
    expect(props.onGoalChange).toHaveBeenCalledWith(nextGoal);
  });

  it("shows validation errors without calling the API", async () => {
    renderDialog({ goal: null });
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalled());

    fireEvent.change(screen.getByTestId("goal-objective"), { target: { value: "" } });
    fireEvent.click(screen.getByTestId("goal-save"));
    expect(await screen.findByText("Goal objective cannot be empty.")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("goal-objective"), {
      target: { value: "Do the work" },
    });
    fireEvent.change(screen.getByTestId("goal-token-budget"), { target: { value: "1.5" } });
    fireEvent.click(screen.getByTestId("goal-save"));
    expect(
      await screen.findByText("Token budget must be a positive whole number."),
    ).toBeInTheDocument();
    expect(mockSetGoal).not.toHaveBeenCalled();
  });

  it("clears and pauses or resumes goals", async () => {
    const { onGoalChange } = renderDialog();
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId("goal-pause"));
    await waitFor(() => expect(mockUpdateGoalStatus).toHaveBeenCalledWith("conv_codex", "paused"));

    cleanup();
    renderDialog({ goal: { ...ACTIVE_GOAL, status: "blocked" }, onGoalChange });
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByTestId("goal-resume"));
    await waitFor(() =>
      expect(mockUpdateGoalStatus).toHaveBeenLastCalledWith("conv_codex", "active"),
    );

    fireEvent.click(screen.getByTestId("goal-clear"));
    await waitFor(() => expect(mockClearGoal).toHaveBeenCalledWith("conv_codex"));
    expect(onGoalChange).toHaveBeenLastCalledWith(null);
  });

  it("disables write actions in read-only mode", async () => {
    renderDialog({ readOnly: true });
    await waitFor(() => expect(mockGetGoal).toHaveBeenCalled());

    expect(screen.getByTestId("goal-objective")).toBeDisabled();
    expect(screen.getByTestId("goal-token-budget")).toBeDisabled();
    expect(screen.getByTestId("goal-save")).toBeDisabled();
    expect(screen.getByTestId("goal-clear")).toBeDisabled();
    expect(screen.getByTestId("goal-pause")).toBeDisabled();
  });

  it("surfaces API errors", async () => {
    mockGetGoal.mockRejectedValueOnce(new Error("runner is asleep"));
    renderDialog({ goal: null });

    expect(await screen.findByText("Could not read goal: runner is asleep")).toBeInTheDocument();
  });
});

describe("goal budget parsing", () => {
  it("returns null for blank budgets and parses positive safe integers", () => {
    expect(parseGoalBudget(" ")).toBeNull();
    expect(parseGoalBudget("40000")).toBe(40000);
  });

  it("rejects non-positive, fractional, and unsafe budgets", () => {
    expect(() => parseGoalBudget("0")).toThrow(/positive whole number/);
    expect(() => parseGoalBudget("1.5")).toThrow(/positive whole number/);
    expect(() => parseGoalBudget("9007199254740992")).toThrow(/positive whole number/);
  });
});
