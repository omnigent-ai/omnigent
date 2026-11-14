import type * as GoalApiModule from "@/lib/goalApi";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GoalDialog, parseGoalBudget } from "./GoalDialog";
import { useGoalState } from "./useGoalState";
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
const mockRefreshGoal = vi.fn<() => Promise<void>>();
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
      refreshGoal={mockRefreshGoal}
      goalRevision={0}
      goal={goal}
      onGoalChange={onGoalChange}
    />,
  );
  return { onGoalChange, onOpenChange };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function ManagedDialog({ enabled = true, open = true }: { enabled?: boolean; open?: boolean }) {
  const {
    goal,
    goalRevision,
    setGoal: setGoalState,
    refreshGoal,
  } = useGoalState("conv_codex", enabled);
  return (
    <>
      <output data-testid="owner-goal">{goal?.objective ?? "No goal"}</output>
      <GoalDialog
        open={open}
        onOpenChange={vi.fn()}
        conversationId="conv_codex"
        readOnly={false}
        goal={goal}
        goalRevision={goalRevision}
        onGoalChange={setGoalState}
        refreshGoal={refreshGoal}
      />
    </>
  );
}

const mutations = [
  { action: "save", button: "goal-save", errorPrefix: "Could not set goal" },
  { action: "clear", button: "goal-clear", errorPrefix: "Could not clear goal" },
  { action: "pause", button: "goal-pause", errorPrefix: "Could not pause goal" },
] as const;

interface GoalMutationResponse {
  goal: Goal | null;
  cleared: boolean;
}

function queueGoalMutations(
  action: (typeof mutations)[number]["action"],
  ...responses: Promise<GoalMutationResponse>[]
) {
  const mock = { save: mockSetGoal, clear: mockClearGoal, pause: mockUpdateGoalStatus }[action];
  for (const response of responses) mock.mockReturnValueOnce(response);
}

const goalReadResults = [
  { result: "a goal", goal: ACTIVE_GOAL },
  { result: "no goal", goal: null },
];

describe("GoalDialog", () => {
  beforeEach(() => {
    mockGetGoal.mockResolvedValue({ goal: ACTIVE_GOAL });
    mockRefreshGoal.mockResolvedValue(undefined);
    mockSetGoal.mockResolvedValue({ goal: ACTIVE_GOAL });
    mockClearGoal.mockResolvedValue({ cleared: true });
    mockUpdateGoalStatus.mockImplementation(async (_sessionId, status) => ({
      goal: { ...ACTIVE_GOAL, status },
    }));
  });

  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("refreshes the current goal through its snapshot owner when opened", async () => {
    const { onGoalChange } = renderDialog({ goal: null });

    expect(screen.getByText("Loading goal")).toBeInTheDocument();
    await waitFor(() => expect(mockRefreshGoal).toHaveBeenCalledWith());
    expect(onGoalChange).not.toHaveBeenCalled();
  });

  it("displays the current goal summary", async () => {
    renderDialog();

    await waitFor(() => expect(mockRefreshGoal).toHaveBeenCalledWith());
    expect(screen.getByTestId("goal-current")).toHaveTextContent("Ship goal mode");
    expect(screen.getByTestId("goal-current")).toHaveTextContent("1,200 / 40,000 tokens / 2 min");
  });

  it("lets an unbreakable objective wrap inside the summary card", async () => {
    renderDialog();

    await waitFor(() => expect(mockRefreshGoal).toHaveBeenCalledWith());
    const objective = within(screen.getByTestId("goal-current")).getByText("Ship goal mode");
    expect(objective).toHaveClass("wrap-anywhere");
  });

  it("gives the scroll region room for the fields' focus ring", async () => {
    renderDialog();

    await waitFor(() => expect(mockRefreshGoal).toHaveBeenCalledWith());
    const scrollRegion = screen.getByTestId("goal-objective").closest(".overflow-y-auto");
    if (!scrollRegion) throw new Error("goal scroll region not found");
    expect(scrollRegion).toHaveClass("p-1", "-m-1");
  });

  it("saves a trimmed objective, token budget, and selected status", async () => {
    const updatedGoal = { ...ACTIVE_GOAL, objective: "Finish tests", status: "paused" };
    mockSetGoal.mockResolvedValueOnce({ goal: updatedGoal });
    const { onGoalChange } = renderDialog({ goal: null });
    await waitFor(() => expect(mockRefreshGoal).toHaveBeenCalled());

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
      refreshGoal: mockRefreshGoal,
      goalRevision: 0,
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
        refreshGoal: mockRefreshGoal,
        goalRevision: 0,
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
        refreshGoal: mockRefreshGoal,
        goalRevision: 0,
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
      mockRefreshGoal.mockRejectedValueOnce(new Error("runner unavailable"));

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
      refreshGoal: mockRefreshGoal,
      goalRevision: 0,
      goal: ACTIVE_GOAL,
      onGoalChange: vi.fn(),
      onOpenChange: vi.fn(),
    };
    const { rerender } = render(<GoalDialog {...props} />);
    await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
    rerender(<GoalDialog {...props} open={false} />);
    mockRefreshGoal.mockRejectedValueOnce(new Error("runner unavailable"));
    rerender(<GoalDialog {...props} goal={null} />);

    await screen.findByText("Could not read goal: runner unavailable");
    expect(screen.getByTestId("goal-objective")).toHaveValue("");
    expect(screen.getByTestId("goal-token-budget")).toHaveValue(null);
    fireEvent.click(screen.getByTestId("goal-save"));
    expect(screen.getByText("Goal objective cannot be empty.")).toBeInTheDocument();
    expect(mockSetGoal).not.toHaveBeenCalled();
  });

  it.each([ACTIVE_GOAL, null])(
    "keeps the newer poll when opening-read responses finish in the same batch (%j)",
    async (nextGoal) => {
      const openingRead = deferred<{ goal: Goal | null }>();
      const pollRead = deferred<{ goal: Goal | null }>();
      mockGetGoal.mockReturnValueOnce(openingRead.promise).mockReturnValueOnce(pollRead.promise);
      render(<ManagedDialog />);
      expect(mockGetGoal).toHaveBeenCalledTimes(2);

      await act(async () => {
        pollRead.resolve({ goal: nextGoal });
        openingRead.resolve({
          goal: {
            ...ACTIVE_GOAL,
            objective: "Obsolete objective",
            tokenBudget: 123,
            status: "paused",
          },
        });
      });

      expect(screen.getByTestId("goal-objective")).toHaveValue(nextGoal?.objective ?? "");
      expect(screen.getByTestId("goal-token-budget")).toHaveValue(nextGoal?.tokenBudget ?? null);
      expect(screen.getByTestId("goal-mode-active")).toHaveAttribute("aria-checked", "true");
      fireEvent.click(screen.getByTestId("goal-save"));
      if (nextGoal) {
        expect(screen.getByTestId("goal-current")).toHaveTextContent(nextGoal.objective);
        await waitFor(() =>
          expect(mockSetGoal).toHaveBeenCalledWith("conv_codex", {
            objective: nextGoal.objective,
            tokenBudget: nextGoal.tokenBudget,
            status: "active",
          }),
        );
      } else {
        expect(screen.getByTestId("goal-empty")).toBeInTheDocument();
        expect(mockSetGoal).not.toHaveBeenCalled();
        expect(screen.getByText("Goal objective cannot be empty.")).toBeInTheDocument();
      }
    },
  );

  it("shows the opening-read error when a competing poll also fails", async () => {
    const openingRead = deferred<{ goal: Goal | null }>();
    const pollRead = deferred<{ goal: Goal | null }>();
    mockGetGoal.mockReturnValueOnce(openingRead.promise).mockReturnValueOnce(pollRead.promise);
    render(<ManagedDialog />);
    expect(mockGetGoal).toHaveBeenCalledTimes(2);

    await act(async () => openingRead.reject(new Error("opening read unavailable")));
    expect(screen.getByText("Could not read goal: opening read unavailable")).toBeInTheDocument();
    await act(async () => pollRead.reject(new Error("poll unavailable")));

    expect(screen.getByText("Could not read goal: opening read unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Loading goal")).not.toBeInTheDocument();
    expect(screen.getByTestId("goal-objective")).toHaveValue("");
  });

  it.each([ACTIVE_GOAL, null])(
    "hides an obsolete opening-read error after a newer poll succeeds (%j)",
    async (nextGoal) => {
      const openingRead = deferred<{ goal: Goal | null }>();
      const pollRead = deferred<{ goal: Goal | null }>();
      mockGetGoal.mockReturnValueOnce(openingRead.promise).mockReturnValueOnce(pollRead.promise);
      render(<ManagedDialog />);
      expect(mockGetGoal).toHaveBeenCalledTimes(2);

      await act(async () => {
        pollRead.resolve({ goal: nextGoal });
        openingRead.reject(new Error("obsolete read unavailable"));
      });

      expect(screen.queryByText(/Could not read goal/)).not.toBeInTheDocument();
      expect(screen.getByTestId("goal-objective")).toHaveValue(nextGoal?.objective ?? "");
      expect(screen.getByTestId("goal-token-budget")).toHaveValue(nextGoal?.tokenBudget ?? null);
      expect(screen.getByTestId("goal-save")).toBeEnabled();
      if (nextGoal)
        expect(screen.getByTestId("goal-current")).toHaveTextContent(nextGoal.objective);
      else expect(screen.getByTestId("goal-empty")).toBeInTheDocument();
    },
  );

  it.each(goalReadResults)(
    "clears a displayed opening-read error when a later poll accepts $result",
    async ({ goal: nextGoal }) => {
      const openingRead = deferred<{ goal: Goal | null }>();
      const pollRead = deferred<{ goal: Goal | null }>();
      mockGetGoal.mockReturnValueOnce(openingRead.promise).mockReturnValueOnce(pollRead.promise);
      render(<ManagedDialog />);
      expect(mockGetGoal).toHaveBeenCalledTimes(2);

      await act(async () => openingRead.reject(new Error("opening read unavailable")));
      expect(screen.getByText("Could not read goal: opening read unavailable")).toBeInTheDocument();
      expect(screen.getByTestId("goal-save")).toBeEnabled();

      await act(async () => pollRead.resolve({ goal: nextGoal }));

      expect(screen.queryByText(/Could not read goal/)).not.toBeInTheDocument();
      expect(screen.getByTestId("goal-objective")).toHaveValue(nextGoal?.objective ?? "");
      expect(screen.getByTestId("goal-token-budget")).toHaveValue(nextGoal?.tokenBudget ?? null);
      expect(screen.getByTestId("goal-save")).toBeEnabled();
      if (nextGoal)
        expect(screen.getByTestId("goal-current")).toHaveTextContent(nextGoal.objective);
      else expect(screen.getByTestId("goal-empty")).toBeInTheDocument();
    },
  );

  it("keeps a displayed opening-read error when an older poll succeeds", async () => {
    const oldPoll = deferred<{ goal: Goal | null }>();
    const openingRead = deferred<{ goal: Goal | null }>();
    mockGetGoal.mockReturnValueOnce(oldPoll.promise).mockReturnValueOnce(openingRead.promise);
    const { rerender } = render(<ManagedDialog open={false} />);
    expect(mockGetGoal).toHaveBeenCalledTimes(1);
    rerender(<ManagedDialog />);
    expect(mockGetGoal).toHaveBeenCalledTimes(2);

    await act(async () => openingRead.reject(new Error("current read unavailable")));
    expect(screen.getByText("Could not read goal: current read unavailable")).toBeInTheDocument();
    await act(async () => oldPoll.resolve({ goal: ACTIVE_GOAL }));

    expect(screen.getByText("Could not read goal: current read unavailable")).toBeInTheDocument();
    expect(screen.getByTestId("goal-empty")).toBeInTheDocument();
    expect(screen.getByTestId("goal-objective")).toHaveValue("");
  });

  it.each(goalReadResults)(
    "preserves a validation error when a poll accepts $result",
    async ({ goal: nextGoal }) => {
      render(<ManagedDialog />);
      await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
      fireEvent.change(screen.getByTestId("goal-objective"), { target: { value: " " } });
      fireEvent.click(screen.getByTestId("goal-save"));
      expect(screen.getByText("Goal objective cannot be empty.")).toBeInTheDocument();
      mockGetGoal.mockResolvedValueOnce({ goal: nextGoal });

      await act(async () => window.dispatchEvent(new Event("focus")));

      expect(screen.getByText("Goal objective cannot be empty.")).toBeInTheDocument();
      expect(screen.getByTestId("goal-objective")).toHaveValue(" ");
      expect(mockSetGoal).not.toHaveBeenCalled();
    },
  );

  it.each(
    mutations.flatMap((mutation) => goalReadResults.map((read) => ({ ...mutation, ...read }))),
  )(
    "preserves a $action error and dirty draft when a poll accepts $result",
    async ({ action, button, errorPrefix, goal: nextGoal }) => {
      const pending = deferred<GoalMutationResponse>();
      queueGoalMutations(action, pending.promise);
      render(<ManagedDialog />);
      await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
      fireEvent.change(screen.getByTestId("goal-objective"), {
        target: { value: "Unsaved objective" },
      });
      fireEvent.click(screen.getByTestId(button));
      await act(async () => pending.reject(new Error("mutation unavailable")));
      expect(screen.getByText(`${errorPrefix}: mutation unavailable`)).toBeInTheDocument();
      mockGetGoal.mockResolvedValueOnce({ goal: nextGoal });

      await act(async () => window.dispatchEvent(new Event("focus")));

      expect(screen.getByText(`${errorPrefix}: mutation unavailable`)).toBeInTheDocument();
      expect(screen.getByTestId("goal-objective")).toHaveValue("Unsaved objective");
      expect(screen.getByTestId("goal-save")).toBeEnabled();
    },
  );

  it.each(
    mutations.flatMap((mutation) =>
      [false, true].map((refreshFirst) => ({ ...mutation, refreshFirst })),
    ),
  )(
    "refreshes current state after a stale closed-dialog $action success (refreshFirst=$refreshFirst)",
    async ({ action, button, refreshFirst }) => {
      const oldRequest = deferred<GoalMutationResponse>();
      const newerRequest = deferred<GoalMutationResponse>();
      const newestRequest = deferred<GoalMutationResponse>();
      const refreshRead = deferred<{ goal: Goal | null }>();
      queueGoalMutations(action, oldRequest.promise);
      queueGoalMutations("save", newerRequest.promise, newestRequest.promise);
      const { rerender } = render(<ManagedDialog />);
      await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
      fireEvent.click(screen.getByTestId(button));
      rerender(<ManagedDialog open={false} />);
      rerender(<ManagedDialog />);
      await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());

      const newerGoal = { ...ACTIVE_GOAL, objective: "Newer accepted goal" };
      fireEvent.change(screen.getByTestId("goal-objective"), {
        target: { value: newerGoal.objective },
      });
      fireEvent.click(screen.getByTestId("goal-save"));
      await act(async () => newerRequest.resolve({ goal: newerGoal, cleared: true }));
      expect(screen.getByTestId("owner-goal")).toHaveTextContent(newerGoal.objective);

      const newestGoal = { ...ACTIVE_GOAL, objective: "Newest accepted goal" };
      fireEvent.change(screen.getByTestId("goal-objective"), {
        target: { value: newestGoal.objective },
      });
      fireEvent.click(screen.getByTestId("goal-save"));
      const readCount = mockGetGoal.mock.calls.length;
      mockGetGoal.mockReturnValueOnce(refreshRead.promise);
      await act(async () => oldRequest.resolve({ goal: ACTIVE_GOAL, cleared: true }));

      expect(mockGetGoal).toHaveBeenCalledTimes(readCount + 1);
      expect(screen.getByTestId("owner-goal")).toHaveTextContent(newerGoal.objective);
      expect(screen.getByTestId("goal-objective")).toHaveValue(newestGoal.objective);
      expect(screen.getByTestId("goal-save")).toBeDisabled();

      const serverGoal = { ...ACTIVE_GOAL, objective: "Current server goal" };
      if (refreshFirst) {
        await act(async () => refreshRead.resolve({ goal: serverGoal }));
        expect(screen.getByTestId("owner-goal")).toHaveTextContent(serverGoal.objective);
        expect(screen.getByTestId("goal-objective")).toHaveValue(newestGoal.objective);
        expect(screen.getByTestId("goal-save")).toBeDisabled();
        await act(async () => newestRequest.resolve({ goal: newestGoal, cleared: true }));
      } else {
        await act(async () => newestRequest.resolve({ goal: newestGoal, cleared: true }));
        await act(async () => refreshRead.resolve({ goal: serverGoal }));
      }

      expect(screen.getByTestId("owner-goal")).toHaveTextContent(newestGoal.objective);
      expect(screen.getByTestId("goal-objective")).toHaveValue(newestGoal.objective);
      expect(screen.getByTestId("goal-save")).toBeEnabled();
    },
  );

  describe.each(["success", "error"] as const)("old %s after runner reconnect", (outcome) => {
    it.each(mutations)(
      "preserves the new draft and busy $action operation",
      async ({ action, button }) => {
        const oldRequest = deferred<{ goal: Goal | null; cleared: boolean }>();
        const newRequest = deferred<{ goal: Goal | null; cleared: boolean }>();
        queueGoalMutations(action, oldRequest.promise, newRequest.promise);
        const { rerender } = render(<ManagedDialog />);
        await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
        fireEvent.click(screen.getByTestId(button));

        rerender(<ManagedDialog enabled={false} />);
        await screen.findByText("Could not read goal: Goal is unavailable for this session.");
        rerender(<ManagedDialog />);
        await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
        const newGoal = {
          ...ACTIVE_GOAL,
          objective: "Draft after reconnect",
          tokenBudget: 999,
          status: "paused",
        };
        fireEvent.change(screen.getByTestId("goal-objective"), {
          target: { value: newGoal.objective },
        });
        fireEvent.change(screen.getByTestId("goal-token-budget"), { target: { value: "999" } });
        fireEvent.click(screen.getByTestId("goal-mode-paused"));
        fireEvent.click(screen.getByTestId(button));

        await act(async () => {
          if (outcome === "success") oldRequest.resolve({ goal: ACTIVE_GOAL, cleared: true });
          else oldRequest.reject(new Error("old mutation failed"));
        });
        const backgroundGoal = {
          ...ACTIVE_GOAL,
          objective: "Current background goal",
          tokenBudget: 888,
        };
        mockGetGoal.mockResolvedValueOnce({ goal: backgroundGoal });
        await act(async () => window.dispatchEvent(new Event("focus")));

        expect(screen.getByTestId("goal-current")).toHaveTextContent(backgroundGoal.objective);
        expect(screen.getByTestId("goal-objective")).toHaveValue(newGoal.objective);
        expect(screen.getByTestId("goal-token-budget")).toHaveValue(999);
        expect(screen.getByTestId("goal-mode-paused")).toHaveAttribute("aria-checked", "true");
        expect(screen.getByTestId("goal-save")).toBeDisabled();
        expect(screen.queryByText(/old mutation failed/)).not.toBeInTheDocument();

        await act(async () => newRequest.resolve({ goal: newGoal, cleared: true }));
        expect(screen.getByTestId("goal-save")).toBeEnabled();
        if (action === "clear") expect(screen.getByTestId("goal-empty")).toBeInTheDocument();
        else expect(screen.getByTestId("goal-current")).toHaveTextContent(newGoal.objective);
      },
    );
  });

  describe.each(["session switch", "reopen"] as const)(
    "pending operations across %s",
    (boundary) => {
      it.each(mutations)(
        "ignores an old $action response without erasing current drafts",
        async ({ action, button }) => {
          const pending = deferred<{ goal: Goal | null; cleared: boolean }>();
          queueGoalMutations(action, pending.promise);
          const props = {
            open: true,
            conversationId: "conv_codex",
            readOnly: false,
            refreshGoal: mockRefreshGoal,
            goalRevision: 0,
            goal: ACTIVE_GOAL,
            onGoalChange: vi.fn(),
            onOpenChange: vi.fn(),
          };
          const { rerender } = render(<GoalDialog {...props} />);
          await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
          fireEvent.click(screen.getByTestId(button));
          expect(screen.getByTestId("goal-save")).toBeDisabled();

          const nextGoal = { ...ACTIVE_GOAL, objective: "Current goal", tokenBudget: 777 };
          const nextProps = {
            ...props,
            conversationId: boundary === "reopen" ? props.conversationId : "conv_other",
            goal: nextGoal,
          };
          mockRefreshGoal.mockResolvedValueOnce(undefined);
          if (boundary === "reopen") rerender(<GoalDialog {...props} open={false} />);
          rerender(<GoalDialog {...nextProps} />);
          await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
          fireEvent.change(screen.getByTestId("goal-objective"), {
            target: { value: "Current unsaved objective" },
          });
          fireEvent.change(screen.getByTestId("goal-token-budget"), { target: { value: "999" } });
          fireEvent.click(screen.getByTestId("goal-mode-paused"));
          await act(async () =>
            pending.resolve({ goal: { ...ACTIVE_GOAL, status: "paused" }, cleared: true }),
          );

          rerender(
            <GoalDialog
              {...nextProps}
              goal={{ ...nextGoal, objective: "Background update", tokenBudget: 888 }}
            />,
          );

          expect(screen.getByTestId("goal-save")).toBeEnabled();
          expect(screen.getByTestId("goal-objective")).toHaveValue("Current unsaved objective");
          expect(screen.getByTestId("goal-token-budget")).toHaveValue(999);
          expect(screen.getByTestId("goal-mode-paused")).toHaveAttribute("aria-checked", "true");
          expect(props.onGoalChange).not.toHaveBeenCalled();
        },
      );

      it.each(mutations)(
        "ignores an old $action error and leaves a newer operation busy",
        async ({ action, button }) => {
          const oldRequest = deferred<{ goal: Goal | null; cleared: boolean }>();
          const newRequest = deferred<{ goal: Goal | null; cleared: boolean }>();
          queueGoalMutations(action, oldRequest.promise, newRequest.promise);
          const props = {
            open: true,
            conversationId: "conv_codex",
            readOnly: false,
            refreshGoal: mockRefreshGoal,
            goalRevision: 0,
            goal: ACTIVE_GOAL,
            onGoalChange: vi.fn(),
            onOpenChange: vi.fn(),
          };
          const { rerender } = render(<GoalDialog {...props} />);
          await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
          fireEvent.click(screen.getByTestId(button));
          if (boundary === "reopen") rerender(<GoalDialog {...props} open={false} />);
          const nextProps = {
            ...props,
            conversationId: boundary === "reopen" ? props.conversationId : "conv_other",
          };
          rerender(<GoalDialog {...nextProps} />);
          await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
          fireEvent.change(screen.getByTestId("goal-objective"), {
            target: { value: "New session draft" },
          });
          fireEvent.click(screen.getByTestId(button));

          await act(async () => oldRequest.reject(new Error("old operation failed")));

          expect(screen.queryByText(/old operation failed/)).not.toBeInTheDocument();
          expect(screen.getByTestId("goal-save")).toBeDisabled();
          expect(screen.getByTestId("goal-objective")).toHaveValue("New session draft");
          await act(async () => newRequest.resolve({ goal: ACTIVE_GOAL, cleared: true }));
          expect(screen.getByTestId("goal-save")).toBeEnabled();
        },
      );
    },
  );

  it("clears the loading state when the session disappears during the opening read", async () => {
    const pending = deferred<void>();
    mockRefreshGoal.mockReturnValueOnce(pending.promise);
    const props = {
      open: true,
      conversationId: "conv_codex",
      readOnly: false,
      refreshGoal: mockRefreshGoal,
      goalRevision: 0,
      goal: ACTIVE_GOAL,
      onGoalChange: vi.fn(),
      onOpenChange: vi.fn(),
    };
    const { rerender } = render(<GoalDialog {...props} />);
    expect(screen.getByText("Loading goal")).toBeInTheDocument();

    rerender(<GoalDialog {...props} conversationId={null} goal={null} />);

    expect(screen.queryByText("Loading goal")).not.toBeInTheDocument();
    await act(async () => pending.resolve(undefined));
    expect(props.onGoalChange).not.toHaveBeenCalled();
    expect(screen.getByTestId("goal-objective")).toHaveValue("");
  });

  it("shows validation errors without calling the API", async () => {
    renderDialog({ goal: null });
    await waitFor(() => expect(mockRefreshGoal).toHaveBeenCalled());

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
    await waitFor(() => expect(mockRefreshGoal).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId("goal-pause"));
    await waitFor(() => expect(mockUpdateGoalStatus).toHaveBeenCalledWith("conv_codex", "paused"));

    cleanup();
    renderDialog({ goal: { ...ACTIVE_GOAL, status: "blocked" }, onGoalChange });
    await waitFor(() => expect(mockRefreshGoal).toHaveBeenCalledTimes(2));
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
    await waitFor(() => expect(mockRefreshGoal).toHaveBeenCalled());

    expect(screen.getByTestId("goal-objective")).toBeDisabled();
    expect(screen.getByTestId("goal-token-budget")).toBeDisabled();
    expect(screen.getByTestId("goal-save")).toBeDisabled();
    expect(screen.getByTestId("goal-clear")).toBeDisabled();
    expect(screen.getByTestId("goal-pause")).toBeDisabled();
  });

  it("surfaces API errors", async () => {
    mockRefreshGoal.mockRejectedValueOnce(new Error("runner is asleep"));
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
