import type * as GoalApiModule from "@/lib/goalApi";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GoalDialog, parseGoalBudget, type GoalDialogProps } from "./GoalDialog";
import { useGoalState } from "./useGoalState";
import type { Goal } from "@/lib/goalApi";
import * as basePath from "@/lib/basePath";
import * as host from "@/lib/host";
import * as identity from "@/lib/identity";
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

function renderDialog(overrides: Partial<GoalDialogProps> = {}) {
  const props = {
    open: true,
    conversationId: "conv_codex",
    readOnly: false,
    refreshGoal: mockRefreshGoal,
    goalRevision: 0,
    goal: ACTIVE_GOAL,
    onGoalChange: vi.fn(),
    onOpenChange: vi.fn(),
    ...overrides,
  };
  return {
    ...render(<GoalDialog {...props} />),
    props,
    onGoalChange: props.onGoalChange,
    onOpenChange: props.onOpenChange,
  };
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

function ManagedDialog({
  enabled = true,
  pollingEnabled = true,
  open = true,
}: {
  enabled?: boolean;
  pollingEnabled?: boolean;
  open?: boolean;
}) {
  const {
    goal,
    goalRevision,
    setGoal: setGoalState,
    refreshGoal,
  } = useGoalState("conv_codex", enabled, pollingEnabled);
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
  return mock;
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
    vi.restoreAllMocks();
    vi.resetAllMocks();
    vi.useRealTimers();
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

  it.each(["all fields", "budget only"])(
    "keeps unsaved %s edits when background goal snapshots change",
    async (editedFields) => {
      const { props, rerender } = renderDialog();
      await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
      if (editedFields === "all fields") {
        fireEvent.change(screen.getByTestId("goal-objective"), {
          target: { value: "My unsaved objective" },
        });
        fireEvent.click(screen.getByTestId("goal-mode-paused"));
      }
      fireEvent.change(screen.getByTestId("goal-token-budget"), {
        target: { value: "12345" },
      });

      for (const goal of [
        { ...ACTIVE_GOAL },
        {
          ...ACTIVE_GOAL,
          objective: "Updated native objective",
          status: "complete",
          tokensUsed: 2000,
        },
      ]) {
        rerender(<GoalDialog {...props} goal={goal} />);
        expect(screen.getByTestId("goal-objective")).toHaveValue(
          editedFields === "all fields" ? "My unsaved objective" : goal.objective,
        );
        expect(screen.getByTestId("goal-token-budget")).toHaveValue(12345);
        const untouchedMode = goal.status === "active" ? "active" : "keep";
        const mode = editedFields === "all fields" ? "paused" : untouchedMode;
        expect(screen.getByTestId(`goal-mode-${mode}`)).toHaveAttribute("aria-checked", "true");
      }
      expect(screen.getByTestId("goal-current")).toHaveTextContent("complete");
      expect(screen.getByTestId("goal-current")).toHaveTextContent("2,000");
      fireEvent.click(screen.getByTestId("goal-save"));
      await waitFor(() =>
        expect(mockSetGoal).toHaveBeenCalledWith("conv_codex", {
          objective:
            editedFields === "all fields" ? "My unsaved objective" : "Updated native objective",
          tokenBudget: 12345,
          status: editedFields === "all fields" ? "paused" : undefined,
        }),
      );
    },
  );

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
      const { props, rerender } = renderDialog();
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

  it.each([
    "reopen",
    "session switch",
    "server switch",
    "anonymous embed switch",
    "base path switch",
  ])("discards old drafts on %s even when the new read fails", async (boundary) => {
    const server = vi.spyOn(host, "getOmnigentServerIdentity").mockReturnValue("server-a");
    const secondFetcher = vi.fn<() => Promise<Response>>();
    const config = vi
      .spyOn(host, "getOmnigentHostConfig")
      .mockReturnValue({ fetcher: vi.fn<() => Promise<Response>>() });
    const path = vi.spyOn(basePath, "getBasePath").mockReturnValue("/first");
    if (boundary === "anonymous embed switch") server.mockReturnValue(null);
    const { props, rerender } = renderDialog();
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
    const nextId = boundary === "session switch" ? "conv_other" : props.conversationId;
    if (boundary === "reopen") rerender(<GoalDialog {...props} open={false} />);
    if (boundary === "server switch") server.mockReturnValue("server-b");
    if (boundary === "anonymous embed switch") config.mockReturnValue({ fetcher: secondFetcher });
    if (boundary === "base path switch") path.mockReturnValue("/second");
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
  });

  it("does not restore a deleted goal after reopening with a failed read", async () => {
    const { props, rerender } = renderDialog();
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

  it.each(
    goalReadResults.flatMap((read) =>
      ["same batch", "poll first"].map((ordering) => ({ ...read, ordering })),
    ),
  )(
    "keeps the newer $result while the opening read settles ($ordering)",
    async ({ goal: nextGoal, ordering }) => {
      const openingRead = deferred<{ goal: Goal | null }>();
      const pollRead = deferred<{ goal: Goal | null }>();
      mockGetGoal.mockReturnValueOnce(openingRead.promise).mockReturnValueOnce(pollRead.promise);
      render(<ManagedDialog />);
      expect(mockGetGoal).toHaveBeenCalledTimes(2);

      if (ordering === "poll first") {
        await act(async () => pollRead.resolve({ goal: nextGoal }));
        expect(screen.queryByText("Loading goal")).not.toBeInTheDocument();
        expect(screen.getByTestId("goal-objective")).toBeEnabled();
        expect(screen.getByTestId("goal-save")).toBeEnabled();
        expect(screen.getByTestId("goal-objective")).toHaveValue(nextGoal?.objective ?? "");
      }
      await act(async () => {
        if (ordering === "same batch") pollRead.resolve({ goal: nextGoal });
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

  it("retains a strict read error and drafts until a confirmed background null arrives", async () => {
    vi.useFakeTimers();
    const actualGoalApi = await vi.importActual<typeof GoalApiModule>("@/lib/goalApi");
    mockGetGoal.mockImplementation(actualGoalApi.getGoal);
    let strictFailure = false;
    let background: object = {
      available: true,
      goal: {
        thread_id: "thread-1",
        objective: ACTIVE_GOAL.objective,
        status: "paused",
        token_budget: ACTIVE_GOAL.tokenBudget,
        tokens_used: ACTIVE_GOAL.tokensUsed,
        time_used_seconds: ACTIVE_GOAL.timeUsedSeconds,
      },
    };
    const fetch = vi
      .spyOn(identity, "authenticatedFetch")
      .mockImplementation(async (_url, init) => {
        if ((init?.method ?? "GET") !== "GET") throw new Error("Unexpected goal write");
        return strictFailure &&
          new Headers(init?.headers).get("X-Omnigent-Codex-Goal-Read") === "strict"
          ? Response.json(
              { error: "codex_native_goal_failed", detail: "runner unavailable" },
              { status: 503 },
            )
          : Response.json(background);
      });
    const { rerender } = render(<ManagedDialog />);
    await act(async () => {});
    fireEvent.change(screen.getByTestId("goal-objective"), {
      target: { value: "Unsaved objective" },
    });
    strictFailure = true;
    rerender(<ManagedDialog pollingEnabled={false} />);
    await act(async () => {});
    expect(screen.getByText("Could not read goal: runner unavailable")).toBeInTheDocument();
    background = { available: false, goal: null };

    rerender(<ManagedDialog />);
    await act(async () => {});
    await act(() => vi.advanceTimersByTimeAsync(5_000));

    expect(screen.getByTestId("owner-goal")).toHaveTextContent(ACTIVE_GOAL.objective);
    expect(screen.getByTestId("goal-current")).toHaveTextContent(ACTIVE_GOAL.objective);
    expect(screen.getByTestId("goal-objective")).toHaveValue("Unsaved objective");
    expect(screen.getByTestId("goal-token-budget")).toHaveValue(ACTIVE_GOAL.tokenBudget);
    expect(screen.getByTestId("goal-mode-paused")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("Could not read goal: runner unavailable")).toBeInTheDocument();
    expect(mockSetGoal).not.toHaveBeenCalled();
    expect(mockUpdateGoalStatus).not.toHaveBeenCalled();
    expect(mockClearGoal).not.toHaveBeenCalled();

    background = { available: true, goal: null };
    await act(() => vi.advanceTimersByTimeAsync(5_000));

    expect(screen.getByTestId("owner-goal")).toHaveTextContent("No goal");
    expect(screen.getByTestId("goal-empty")).toBeInTheDocument();
    expect(screen.queryByText(/Could not read goal/)).not.toBeInTheDocument();
    expect(screen.getByTestId("goal-objective")).toHaveValue("Unsaved objective");
    expect(screen.getByTestId("goal-token-budget")).toHaveValue(null);
    expect(screen.getByTestId("goal-mode-active")).toHaveAttribute("aria-checked", "true");
    expect(fetch.mock.calls.every(([, init]) => (init?.method ?? "GET") === "GET")).toBe(true);
  });

  it.each(
    goalReadResults.flatMap((read) =>
      ["same batch", "error first"].map((ordering) => ({ ...read, ordering })),
    ),
  )(
    "recovers $result after an opening-read failure ($ordering)",
    async ({ goal: nextGoal, ordering }) => {
      const openingRead = deferred<{ goal: Goal | null }>();
      const pollRead = deferred<{ goal: Goal | null }>();
      mockGetGoal.mockReturnValueOnce(openingRead.promise).mockReturnValueOnce(pollRead.promise);
      render(<ManagedDialog />);
      expect(mockGetGoal).toHaveBeenCalledTimes(2);

      if (ordering === "error first") {
        await act(async () => openingRead.reject(new Error("opening read unavailable")));
        expect(
          screen.getByText("Could not read goal: opening read unavailable"),
        ).toBeInTheDocument();
        expect(screen.getByTestId("goal-save")).toBeEnabled();
        await act(async () => pollRead.resolve({ goal: nextGoal }));
      } else {
        await act(async () => {
          pollRead.resolve({ goal: nextGoal });
          openingRead.reject(new Error("obsolete read unavailable"));
        });
      }

      expect(screen.queryByText(/Could not read goal/)).not.toBeInTheDocument();
      expect(screen.getByTestId("goal-objective")).toHaveValue(nextGoal?.objective ?? "");
      expect(screen.getByTestId("goal-token-budget")).toHaveValue(nextGoal?.tokenBudget ?? null);
      expect(screen.getByTestId("goal-save")).toBeEnabled();
      if (nextGoal)
        expect(screen.getByTestId("goal-current")).toHaveTextContent(nextGoal.objective);
      else expect(screen.getByTestId("goal-empty")).toBeInTheDocument();
    },
  );

  it("recovers an opening-read failure when an older pending poll succeeds", async () => {
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

    expect(screen.queryByText(/Could not read goal/)).not.toBeInTheDocument();
    expect(screen.getByTestId("goal-current")).toHaveTextContent(ACTIVE_GOAL.objective);
    expect(screen.getByTestId("goal-objective")).toHaveValue(ACTIVE_GOAL.objective);
    expect(screen.getByTestId("goal-token-budget")).toHaveValue(ACTIVE_GOAL.tokenBudget);
    expect(screen.getByTestId("goal-save")).toBeEnabled();
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
    "preserves a $action error, draft, and untouched fields through reconnect and a poll accepting $result",
    async ({ action, button, errorPrefix, goal: nextGoal }) => {
      const pending = deferred<GoalMutationResponse>();
      queueGoalMutations(action, pending.promise);
      const reconnectRead = deferred<{ goal: Goal | null }>();
      const { rerender } = render(<ManagedDialog />);
      await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
      fireEvent.change(screen.getByTestId("goal-objective"), {
        target: { value: "Unsaved objective" },
      });
      fireEvent.click(screen.getByTestId(button));
      await act(async () => pending.reject(new Error("mutation unavailable")));
      expect(screen.getByText(`${errorPrefix}: mutation unavailable`)).toBeInTheDocument();
      mockGetGoal.mockReturnValueOnce(reconnectRead.promise);
      rerender(<ManagedDialog pollingEnabled={false} />);
      expect(screen.getByText(`${errorPrefix}: mutation unavailable`)).toBeInTheDocument();
      expect(screen.getByTestId("owner-goal")).toHaveTextContent(ACTIVE_GOAL.objective);
      expect(screen.getByTestId("goal-objective")).toHaveValue("Unsaved objective");
      expect(screen.getByTestId("goal-token-budget")).toHaveValue(ACTIVE_GOAL.tokenBudget);
      expect(screen.getByTestId("goal-mode-active")).toHaveAttribute("aria-checked", "true");
      expect(screen.getByTestId("goal-save")).toBeDisabled();
      await act(async () => reconnectRead.reject(new Error("reconnect unavailable")));
      expect(screen.getByText(`${errorPrefix}: mutation unavailable`)).toBeInTheDocument();
      expect(screen.getByTestId("owner-goal")).toHaveTextContent(ACTIVE_GOAL.objective);
      expect(screen.getByTestId("goal-token-budget")).toHaveValue(ACTIVE_GOAL.tokenBudget);
      mockGetGoal.mockResolvedValue({ goal: nextGoal });
      rerender(<ManagedDialog />);

      await act(async () => window.dispatchEvent(new Event("focus")));

      expect(screen.getByText(`${errorPrefix}: mutation unavailable`)).toBeInTheDocument();
      expect(screen.getByTestId("goal-objective")).toHaveValue("Unsaved objective");
      expect(screen.getByTestId("goal-save")).toBeEnabled();
    },
  );

  it.each(
    mutations.flatMap((mutation) =>
      ["reopen", "remount"].flatMap((boundary) =>
        ["success", "error", "read error"].map((outcome) => ({ ...mutation, boundary, outcome })),
      ),
    ),
  )(
    "blocks another $action across $boundary until settlement and reconciliation ($outcome)",
    async ({ action, button, boundary, outcome }) => {
      const oldRequest = deferred<GoalMutationResponse>();
      const nextRequest = deferred<GoalMutationResponse>();
      const refreshRead = deferred<{ goal: Goal | null }>();
      try {
        queueGoalMutations(action, oldRequest.promise);
        queueGoalMutations("save", nextRequest.promise);
        const { rerender } = render(<ManagedDialog />);
        await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
        fireEvent.click(screen.getByTestId(button));
        if (boundary === "reopen") {
          rerender(<ManagedDialog open={false} />);
          rerender(<ManagedDialog />);
        } else {
          rerender(<ManagedDialog key="replacement" />);
        }
        await waitFor(() => expect(screen.queryByText("Loading goal")).not.toBeInTheDocument());

        // Exercise the write actions after the new mount's opening read has settled.
        for (const { button: mutationButton } of mutations) {
          fireEvent.click(screen.getByTestId(mutationButton));
        }
        expect(mockSetGoal).toHaveBeenCalledTimes(action === "save" ? 1 : 0);
        expect(mockClearGoal).toHaveBeenCalledTimes(action === "clear" ? 1 : 0);
        expect(mockUpdateGoalStatus).toHaveBeenCalledTimes(action === "pause" ? 1 : 0);
        expect(screen.getByTestId("goal-save")).toBeDisabled();
        expect(screen.getByTestId("goal-objective")).toHaveValue(ACTIVE_GOAL.objective);
        expect(screen.getByTestId("goal-token-budget")).toHaveValue(ACTIVE_GOAL.tokenBudget);
        expect(screen.getByTestId("goal-mode-active")).toHaveAttribute("aria-checked", "true");

        const readCount = mockGetGoal.mock.calls.length;
        mockGetGoal.mockReturnValueOnce(refreshRead.promise);
        await act(async () => {
          if (outcome === "error") oldRequest.reject(new Error("old operation failed"));
          else
            oldRequest.resolve({
              goal: { ...ACTIVE_GOAL, objective: "Old response" },
              cleared: true,
            });
        });
        expect(mockGetGoal).toHaveBeenCalledTimes(readCount + 1);
        fireEvent.click(screen.getByTestId("goal-save"));
        expect(mockSetGoal).toHaveBeenCalledTimes(action === "save" ? 1 : 0);
        expect(screen.getByTestId("goal-save")).toBeDisabled();
        expect(screen.getByTestId("owner-goal")).toHaveTextContent(ACTIVE_GOAL.objective);
        expect(screen.getByTestId("goal-objective")).toHaveValue(ACTIVE_GOAL.objective);
        expect(screen.queryByText(/old operation failed/)).not.toBeInTheDocument();

        const serverGoal = { ...ACTIVE_GOAL, objective: "Current server goal" };
        await act(async () => {
          if (outcome === "read error") refreshRead.reject(new Error("reconciliation unavailable"));
          else refreshRead.resolve({ goal: serverGoal });
        });
        expect(screen.getByTestId("goal-save")).toBeEnabled();
        if (outcome === "read error") {
          expect(
            screen.getByText("Could not read goal: reconciliation unavailable"),
          ).toBeInTheDocument();
          expect(screen.getByTestId("goal-objective")).toHaveValue(ACTIVE_GOAL.objective);
          expect(screen.getByTestId("goal-token-budget")).toHaveValue(ACTIVE_GOAL.tokenBudget);
        } else {
          expect(screen.getByTestId("owner-goal")).toHaveTextContent(serverGoal.objective);
          expect(screen.getByTestId("goal-objective")).toHaveValue(serverGoal.objective);
        }
        fireEvent.change(screen.getByTestId("goal-objective"), {
          target: { value: "Next local draft" },
        });
        fireEvent.change(screen.getByTestId("goal-token-budget"), { target: { value: "999" } });
        fireEvent.click(screen.getByTestId("goal-mode-paused"));
        fireEvent.click(screen.getByTestId("goal-save"));
        expect(mockSetGoal).toHaveBeenLastCalledWith("conv_codex", {
          objective: "Next local draft",
          tokenBudget: 999,
          status: "paused",
        });
        expect(screen.getByTestId("goal-save")).toBeDisabled();
        expect(screen.getByTestId("goal-objective")).toHaveValue("Next local draft");
        expect(screen.getByTestId("goal-token-budget")).toHaveValue(999);
        expect(screen.getByTestId("goal-mode-paused")).toHaveAttribute("aria-checked", "true");
        await act(async () => nextRequest.resolve({ goal: serverGoal, cleared: true }));
        expect(screen.getByTestId("goal-save")).toBeEnabled();
      } finally {
        await act(async () => {
          oldRequest.resolve({ goal: ACTIVE_GOAL, cleared: true });
          nextRequest.resolve({ goal: ACTIVE_GOAL, cleared: true });
          refreshRead.resolve({ goal: ACTIVE_GOAL });
        });
      }
    },
  );

  it.each(
    [...mutations, { action: "resume", button: "goal-resume" }].flatMap((mutation) =>
      ["success", "error"].map((late) => ({ ...mutation, late })),
    ),
  )(
    "bounds a stalled $action and reconciliation after reopening, ignoring late $late",
    async ({ action, button, late }) => {
      vi.useFakeTimers();
      const actualGoalApi = await vi.importActual<typeof GoalApiModule>("@/lib/goalApi");
      mockGetGoal.mockImplementation(actualGoalApi.getGoal);
      mockSetGoal.mockImplementation(actualGoalApi.setGoal);
      mockClearGoal.mockImplementation(actualGoalApi.clearGoal);
      mockUpdateGoalStatus.mockImplementation(actualGoalApi.updateGoalStatus);
      const oldRequest = deferred<Response>();
      const nextRequest = deferred<Response>();
      const initialGoal = { ...ACTIVE_GOAL, status: action === "resume" ? "paused" : "active" };
      const wireGoal = {
        thread_id: "thread-1",
        objective: initialGoal.objective,
        status: initialGoal.status,
        token_budget: initialGoal.tokenBudget,
        tokens_used: initialGoal.tokensUsed,
        time_used_seconds: initialGoal.timeUsedSeconds,
      };
      const reconciliationBody = deferred<{ goal: typeof wireGoal }>();
      let readCount = 0;
      let writeCount = 0;
      const fetch = vi.spyOn(identity, "authenticatedFetch").mockImplementation((_url, init) => {
        if ((init?.method ?? "GET") !== "GET") {
          writeCount += 1;
          return writeCount === 1 ? oldRequest.promise : nextRequest.promise;
        }
        readCount += 1;
        return Promise.resolve(
          readCount <= 2
            ? Response.json({ goal: wireGoal })
            : ({ ok: true, json: () => reconciliationBody.promise } as Response),
        );
      });
      const { rerender, unmount } = render(<ManagedDialog pollingEnabled={false} />);
      try {
        await act(async () => {});
        expect(screen.getByTestId("goal-save")).toBeEnabled();
        fireEvent.click(screen.getByTestId(button));
        const writeSignal = fetch.mock.calls[1][1]?.signal;
        rerender(<ManagedDialog pollingEnabled={false} open={false} />);
        rerender(<ManagedDialog pollingEnabled={false} />);
        await act(async () => {});
        expect(readCount).toBe(2);
        expect(screen.getByTestId("goal-objective")).toBeDisabled();

        await act(() => vi.advanceTimersByTimeAsync(59_999));
        expect(readCount).toBe(2);
        await act(() => vi.advanceTimersByTimeAsync(1));

        expect(readCount).toBe(3);
        expect(writeCount).toBe(1);
        expect(writeSignal?.aborted).toBe(true);
        const reconciliationSignal = fetch.mock.calls.at(-1)?.[1]?.signal;
        expect(
          fetch.mock.calls
            .filter(([, init]) => (init?.method ?? "GET") === "GET")
            .map(([, init]) => new Headers(init?.headers).get("X-Omnigent-Codex-Goal-Read")),
        ).toEqual(["strict", "strict", "strict"]);
        expect(screen.getByTestId("goal-save")).toBeDisabled();
        await act(() => vi.advanceTimersByTimeAsync(59_999));
        expect(screen.getByTestId("goal-save")).toBeDisabled();
        await act(() => vi.advanceTimersByTimeAsync(1));

        expect(reconciliationSignal?.aborted).toBe(true);
        expect(
          screen.getByText(/Could not read goal: .*uncertain.*refresh.*before.*retry/i),
        ).toBeInTheDocument();
        expect(screen.getByTestId("goal-save")).toBeEnabled();
        fireEvent.change(screen.getByTestId("goal-objective"), {
          target: { value: "Fresh saved objective" },
        });
        fireEvent.change(screen.getByTestId("goal-token-budget"), { target: { value: "789" } });
        fireEvent.click(screen.getByTestId("goal-mode-paused"));
        fireEvent.click(screen.getByTestId("goal-save"));
        expect(writeCount).toBe(2);

        await act(async () => {
          if (late === "error") oldRequest.reject(new Error("late write failure"));
          else
            oldRequest.resolve(
              Response.json({ goal: { ...wireGoal, objective: "Old reply" }, cleared: true }),
            );
          reconciliationBody.resolve({ goal: { ...wireGoal, objective: "Old read reply" } });
        });

        expect(screen.getByTestId("owner-goal")).toHaveTextContent(initialGoal.objective);
        expect(screen.getByTestId("goal-objective")).toHaveValue("Fresh saved objective");
        expect(screen.getByTestId("goal-token-budget")).toHaveValue(789);
        expect(screen.getByTestId("goal-mode-paused")).toHaveAttribute("aria-checked", "true");
        expect(screen.getByTestId("goal-save")).toBeDisabled();
        expect(writeCount).toBe(2);
        expect(readCount).toBe(3);
        await act(async () =>
          nextRequest.resolve(
            Response.json({
              goal: {
                ...wireGoal,
                objective: "Fresh saved objective",
                token_budget: 789,
                status: "paused",
              },
            }),
          ),
        );
        expect(screen.getByTestId("owner-goal")).toHaveTextContent("Fresh saved objective");
        expect(screen.getByTestId("goal-save")).toBeEnabled();
      } finally {
        unmount();
        await act(async () => {
          oldRequest.resolve(Response.json({ goal: wireGoal, cleared: true }));
          nextRequest.resolve(Response.json({ goal: wireGoal }));
          reconciliationBody.resolve({ goal: wireGoal });
        });
        fetch.mockRestore();
      }
    },
  );

  describe.each(["success", "error"] as const)("old %s after runner reconnect", (outcome) => {
    it.each(mutations)(
      "preserves edits and keeps the pending $action busy until its request settles",
      async ({ action, button }) => {
        const oldRequest = deferred<{ goal: Goal | null; cleared: boolean }>();
        const newRequest = deferred<{ goal: Goal | null; cleared: boolean }>();
        try {
          const mutationMock = queueGoalMutations(action, oldRequest.promise, newRequest.promise);
          const { rerender } = render(<ManagedDialog />);
          await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
          const newGoal = {
            ...ACTIVE_GOAL,
            objective: "Draft before reconnect",
            tokenBudget: 999,
            status: "paused",
          };
          fireEvent.change(screen.getByTestId("goal-objective"), {
            target: { value: newGoal.objective },
          });
          fireEvent.change(screen.getByTestId("goal-token-budget"), { target: { value: "999" } });
          fireEvent.click(screen.getByTestId("goal-mode-paused"));
          fireEvent.click(screen.getByTestId(button));

          const readsBeforeReconnect = mockGetGoal.mock.calls.length;
          rerender(<ManagedDialog pollingEnabled={false} />);
          await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(readsBeforeReconnect + 1));
          rerender(<ManagedDialog />);
          await waitFor(() => expect(mockGetGoal).toHaveBeenCalledTimes(readsBeforeReconnect + 3));
          await waitFor(() => expect(screen.queryByText("Loading goal")).not.toBeInTheDocument());

          expect(screen.getByTestId("goal-objective")).toHaveValue(newGoal.objective);
          expect(screen.getByTestId("goal-token-budget")).toHaveValue(999);
          expect(screen.getByTestId("goal-mode-paused")).toHaveAttribute("aria-checked", "true");
          for (const { button: mutationButton } of mutations) {
            expect(screen.getByTestId(mutationButton)).toBeDisabled();
            fireEvent.click(screen.getByTestId(mutationButton));
          }
          expect(mockSetGoal).toHaveBeenCalledTimes(action === "save" ? 1 : 0);
          expect(mockClearGoal).toHaveBeenCalledTimes(action === "clear" ? 1 : 0);
          expect(mockUpdateGoalStatus).toHaveBeenCalledTimes(action === "pause" ? 1 : 0);

          const backgroundGoal = {
            ...ACTIVE_GOAL,
            objective: "Current background goal",
            tokenBudget: 888,
          };
          const readsBeforeOldResponse = mockGetGoal.mock.calls.length;
          mockGetGoal.mockResolvedValueOnce({ goal: backgroundGoal });
          await act(async () => {
            if (outcome === "success") oldRequest.resolve({ goal: ACTIVE_GOAL, cleared: true });
            else oldRequest.reject(new Error("old mutation failed"));
          });
          expect(mockGetGoal).toHaveBeenCalledTimes(readsBeforeOldResponse + 1);
          expect(screen.getByTestId("goal-current")).toHaveTextContent(backgroundGoal.objective);
          expect(screen.getByTestId("goal-objective")).toHaveValue(newGoal.objective);
          expect(screen.getByTestId("goal-token-budget")).toHaveValue(999);
          expect(screen.getByTestId("goal-mode-paused")).toHaveAttribute("aria-checked", "true");
          expect(screen.getByTestId("goal-save")).toBeEnabled();
          expect(screen.queryByText(/old mutation failed/)).not.toBeInTheDocument();

          fireEvent.click(screen.getByTestId(button));
          expect(mutationMock).toHaveBeenCalledTimes(2);
          expect(screen.getByTestId("goal-save")).toBeDisabled();
          await act(async () => newRequest.resolve({ goal: newGoal, cleared: true }));
          expect(screen.getByTestId("goal-save")).toBeEnabled();
          if (action === "clear") expect(screen.getByTestId("goal-empty")).toBeInTheDocument();
          else expect(screen.getByTestId("goal-current")).toHaveTextContent(newGoal.objective);
        } finally {
          await act(async () => {
            oldRequest.resolve({ goal: ACTIVE_GOAL, cleared: true });
            newRequest.resolve({ goal: ACTIVE_GOAL, cleared: true });
          });
        }
      },
    );
  });

  it.each(mutations)(
    "refreshes its matching snapshot owner after a pending $action succeeds while closed",
    async ({ action, button }) => {
      const pending = deferred<GoalMutationResponse>();
      try {
        queueGoalMutations(action, pending.promise);
        const { props, rerender, onGoalChange } = renderDialog();
        await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
        fireEvent.click(screen.getByTestId(button));
        expect(screen.getByTestId("goal-save")).toBeDisabled();
        rerender(<GoalDialog {...props} open={false} />);
        const readsBeforeSettlement = mockRefreshGoal.mock.calls.length;

        await act(async () => pending.resolve({ goal: ACTIVE_GOAL, cleared: true }));

        expect(mockRefreshGoal).toHaveBeenCalledTimes(readsBeforeSettlement + 1);
        expect(onGoalChange).not.toHaveBeenCalled();
        rerender(<GoalDialog {...props} />);
        await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
      } finally {
        await act(async () => pending.resolve({ goal: ACTIVE_GOAL, cleared: true }));
      }
    },
  );

  describe.each(["session", "server", "anonymous embed"] as const)(
    "pending successful operations across a %s switch",
    (boundary) => {
      it.each(mutations)(
        "ignores an old $action response without erasing current drafts",
        async ({ action, button }) => {
          const server = vi.spyOn(host, "getOmnigentServerIdentity").mockReturnValue("server-a");
          const firstFetcher = vi.fn<() => Promise<Response>>();
          const secondFetcher = vi.fn<() => Promise<Response>>();
          const config = vi
            .spyOn(host, "getOmnigentHostConfig")
            .mockReturnValue({ fetcher: firstFetcher });
          if (boundary === "anonymous embed") server.mockReturnValue(null);
          const pending = deferred<{ goal: Goal | null; cleared: boolean }>();
          const currentRequest = deferred<GoalMutationResponse>();
          try {
            queueGoalMutations(action, pending.promise, currentRequest.promise);
            const { props, rerender } = renderDialog();
            await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
            fireEvent.click(screen.getByTestId(button));
            expect(screen.getByTestId("goal-save")).toBeDisabled();

            const nextRefreshGoal = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);
            const nextGoal = { ...ACTIVE_GOAL, objective: "Current goal", tokenBudget: 777 };
            const nextProps = {
              ...props,
              conversationId: boundary === "session" ? "conv_other" : props.conversationId,
              goal: nextGoal,
              refreshGoal: nextRefreshGoal,
            };
            if (boundary === "server") server.mockReturnValue("server-b");
            if (boundary === "anonymous embed") config.mockReturnValue({ fetcher: secondFetcher });
            rerender(<GoalDialog {...nextProps} />);
            await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
            if (boundary === "server") server.mockReturnValue("server-a");
            if (boundary === "anonymous embed") config.mockReturnValue({ fetcher: firstFetcher });
            rerender(<GoalDialog {...props} />);
            await waitFor(() => expect(screen.queryByText("Loading goal")).not.toBeInTheDocument());
            fireEvent.click(screen.getByTestId(button));
            const mutationMock = {
              save: mockSetGoal,
              clear: mockClearGoal,
              pause: mockUpdateGoalStatus,
            }[action];
            expect(mutationMock).toHaveBeenCalledTimes(1);
            expect(screen.getByTestId("goal-save")).toBeDisabled();
            if (boundary === "server") server.mockReturnValue("server-b");
            if (boundary === "anonymous embed") config.mockReturnValue({ fetcher: secondFetcher });
            rerender(<GoalDialog {...nextProps} />);
            await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
            fireEvent.change(screen.getByTestId("goal-objective"), {
              target: { value: "Current unsaved objective" },
            });
            fireEvent.change(screen.getByTestId("goal-token-budget"), { target: { value: "999" } });
            fireEvent.click(screen.getByTestId("goal-mode-paused"));
            fireEvent.click(screen.getByTestId(button));
            const currentReads = nextRefreshGoal.mock.calls.length;
            const originalReads = mockRefreshGoal.mock.calls.length;
            await act(async () =>
              pending.resolve({ goal: { ...ACTIVE_GOAL, status: "paused" }, cleared: true }),
            );
            expect(nextRefreshGoal).toHaveBeenCalledTimes(currentReads);
            expect(mockRefreshGoal).toHaveBeenCalledTimes(originalReads);

            rerender(
              <GoalDialog
                {...nextProps}
                goal={{ ...nextGoal, objective: "Background update", tokenBudget: 888 }}
              />,
            );

            expect(screen.getByTestId("goal-save")).toBeDisabled();
            expect(screen.getByTestId("goal-objective")).toHaveValue("Current unsaved objective");
            expect(screen.getByTestId("goal-token-budget")).toHaveValue(999);
            expect(screen.getByTestId("goal-mode-paused")).toHaveAttribute("aria-checked", "true");
            expect(props.onGoalChange).not.toHaveBeenCalled();
            await act(async () => currentRequest.resolve({ goal: nextGoal, cleared: true }));
            expect(screen.getByTestId("goal-save")).toBeEnabled();
          } finally {
            await act(async () => {
              pending.resolve({ goal: ACTIVE_GOAL, cleared: true });
              currentRequest.resolve({ goal: ACTIVE_GOAL, cleared: true });
            });
          }
        },
      );
    },
  );

  describe("pending operations across a session switch", () => {
    it.each(mutations)(
      "ignores an old $action error and leaves a newer operation busy",
      async ({ action, button }) => {
        const oldRequest = deferred<GoalMutationResponse>();
        const newRequest = deferred<GoalMutationResponse>();
        try {
          queueGoalMutations(action, oldRequest.promise, newRequest.promise);
          const { props, rerender } = renderDialog();
          await waitFor(() => expect(screen.getByTestId("goal-save")).toBeEnabled());
          fireEvent.click(screen.getByTestId(button));
          const nextProps = { ...props, conversationId: "conv_other" };
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
        } finally {
          await act(async () => {
            oldRequest.resolve({ goal: ACTIVE_GOAL, cleared: true });
            newRequest.resolve({ goal: ACTIVE_GOAL, cleared: true });
          });
        }
      },
    );
  });

  it("clears the loading state when the session disappears during the opening read", async () => {
    const pending = deferred<void>();
    mockRefreshGoal.mockReturnValueOnce(pending.promise);
    const { props, rerender } = renderDialog();
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
