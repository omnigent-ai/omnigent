import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useCanEdit, useIsOwner } from "@/hooks/usePermissions";
import { useSession } from "@/hooks/useSession";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import {
  confirmCodeRuns,
  sha256Hex,
  startCodeRun,
  stopCodeRun,
  type CodeRunLanguage,
} from "@/lib/codeRuns";
import { useChatStore } from "@/store/chatStore";
import { useCodeRunStore } from "@/store/codeRunStore";
import { ConversationScopeContext } from "@/components/chat/conversationScope";
import { CodeBlockRunner, type CodeRunMessage } from "./CodeRunPanel";

vi.mock("@/hooks/useSession", () => ({ useSession: vi.fn() }));
vi.mock("@/hooks/usePermissions", () => ({ useIsOwner: vi.fn(), useCanEdit: vi.fn() }));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: vi.fn() }));
vi.mock("@/lib/codeRuns", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  startCodeRun: vi.fn(),
  stopCodeRun: vi.fn(),
}));

const SESSION = "conv_a";
const CODE = "$ echo hi";
const HASH = sha256Hex(CODE);
const FINISHED: CodeRunMessage = { messageId: "msg_1", streaming: false };

function setSession(overrides: Record<string, unknown> = {}) {
  vi.mocked(useSession).mockReturnValue({
    session: { runnerId: "runner_1", runnerOnline: true, harness: "openai-agents", ...overrides },
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof useSession>);
}

function renderRunner(
  message: CodeRunMessage = FINISHED,
  code = CODE,
  { language = "shell", console = false }: { language?: CodeRunLanguage; console?: boolean } = {},
) {
  return render(
    <ConversationScopeContext.Provider value={SESSION}>
      <CodeBlockRunner
        message={message}
        code={code}
        language={language}
        console={console}
        render={(button?: ReactNode) => <div data-testid="block">{button}</div>}
      />
    </ConversationScopeContext.Provider>,
  );
}

function startedItem(runId: string, startedAt: number) {
  return {
    type: "code_run",
    run_id: runId,
    phase: "started",
    message_id: "msg_1",
    block_hash: HASH,
    language: "shell",
    command: "echo hi",
    started_at: startedAt,
  };
}

function finishedItem(runId: string, extra: Record<string, unknown> = {}) {
  return {
    type: "code_run",
    run_id: runId,
    phase: "finished",
    status: "exited",
    exit_code: 0,
    duration_ms: 1500,
    output: [{ stream: "stdout", text: "hi\n" }],
    truncated: false,
    ...extra,
  };
}

const runButton = () => screen.getByTestId("code-run-button");

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  useCodeRunStore.setState({ sessions: {} });
  useChatStore.setState({ pendingComposerText: [], conversationId: SESSION, sandboxStatus: null });
  setSession();
  vi.mocked(useIsOwner).mockReturnValue(true);
  vi.mocked(useCanEdit).mockReturnValue(true);
  vi.mocked(useIsMobileViewport).mockReturnValue(false);
  vi.mocked(startCodeRun).mockResolvedValue({ ok: true, runId: "run_new" });
  vi.mocked(stopCodeRun).mockResolvedValue();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Run button", () => {
  it("shows for the owner of a session with a runner once the message finished", () => {
    renderRunner();
    expect(runButton()).toBeEnabled();
  });

  it.each([
    ["a viewer", () => vi.mocked(useIsOwner).mockReturnValue(false)],
    ["a session without a runner", () => setSession({ runnerId: null })],
    [
      "a session whose sandbox is relaunching",
      () => useChatStore.setState({ sandboxStatus: { stage: "provisioning", error: null } }),
    ],
  ])("is hidden for %s", (_label, arrange) => {
    arrange();
    renderRunner();
    expect(screen.queryByTestId("code-run-button")).toBeNull();
  });

  it("shows again once the sandbox launch failed or ended", () => {
    useChatStore.setState({ sandboxStatus: { stage: "failed", error: "spend limit reached" } });
    renderRunner();
    expect(runButton()).toBeEnabled();
  });

  it("is hidden while the message is still streaming", () => {
    renderRunner({ messageId: "msg_1", streaming: true });
    expect(screen.queryByTestId("code-run-button")).toBeNull();
  });

  it("is disabled with a reason once the session turned out to use copy-on-write", async () => {
    vi.mocked(startCodeRun).mockResolvedValue({
      ok: false,
      code: "copy_on_write",
      reason: "Unavailable with copy-on-write roots",
    });
    confirmCodeRuns(SESSION);
    renderRunner();
    fireEvent.click(runButton());
    await waitFor(() => expect(runButton()).toBeDisabled());
    expect(runButton().closest("[aria-label]")?.getAttribute("aria-label")).toBe("Run");
  });
});

describe("confirmation", () => {
  const LAUNCHING = { sandboxStatus: { stage: "provisioning" as const, error: null } };

  it("starts nothing when confirmed as the session's sandbox starts launching", async () => {
    renderRunner();
    fireEvent.click(runButton());
    const dialog = await screen.findByTestId("code-run-dialog");
    act(() => {
      useChatStore.setState(LAUNCHING);
      fireEvent.click(within(dialog).getByRole("button", { name: "Run" }));
    });
    await act(async () => {});
    expect(startCodeRun).not.toHaveBeenCalled();
  });

  it("closes when the session's sandbox starts launching", async () => {
    renderRunner();
    fireEvent.click(runButton());
    await screen.findByTestId("code-run-dialog");
    act(() => useChatStore.setState(LAUNCHING));
    await waitFor(() => expect(screen.queryByTestId("code-run-dialog")).toBeNull());
    act(() => useChatStore.setState({ sandboxStatus: null }));
    expect(screen.queryByTestId("code-run-dialog")).toBeNull();
    expect(startCodeRun).not.toHaveBeenCalled();
  });

  it("asks once per session with the exact command, then runs straight away", async () => {
    renderRunner();
    fireEvent.click(runButton());
    const dialog = await screen.findByTestId("code-run-dialog");
    expect(within(dialog).getByTestId("code-run-dialog-code").textContent).toBe(CODE);
    expect(within(dialog).getByTestId("code-run-dialog-commands").textContent).toBe("echo hi");
    expect(within(dialog).getByText("Shell")).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "Runs in this session are saved, visible to anyone who can view it, and sent to the agent on its next turn.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Run" }));
    await waitFor(() =>
      expect(startCodeRun).toHaveBeenCalledWith(SESSION, {
        messageId: "msg_1",
        blockHash: HASH,
        language: "shell",
        code: CODE,
        approved: false,
      }),
    );
    await waitFor(() => expect(screen.queryByTestId("code-run-dialog")).toBeNull());

    fireEvent.click(runButton());
    await waitFor(() => expect(startCodeRun).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("code-run-dialog")).toBeNull();
  });

  it("shows a console block's exact payload, sends it unchanged, and lists the commands", async () => {
    const consoleBlock = "$ ls\nfile.txt\n% pwd\n/tmp";
    const actual = await vi.importActual<Record<string, unknown>>("@/lib/codeRuns");
    const realStartCodeRun = actual.startCodeRun as typeof startCodeRun;
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({ run_id: "run_new" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.mocked(startCodeRun).mockImplementation(realStartCodeRun);
    try {
      renderRunner(FINISHED, consoleBlock, { console: true });
      fireEvent.click(runButton());
      const dialog = await screen.findByTestId("code-run-dialog");
      const shown = within(dialog).getByTestId("code-run-dialog-code").textContent;
      expect(shown).toBe(consoleBlock);
      expect(within(dialog).getByText("Runs these commands")).toBeInTheDocument();
      expect(within(dialog).getByTestId("code-run-dialog-commands").textContent).toBe("ls\npwd");

      fireEvent.click(within(dialog).getByRole("button", { name: "Run" }));
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      expect(JSON.parse(fetchMock.mock.calls[0]![1].body).code).toBe(shown);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("shows no command list for Python, whose source runs as sent", async () => {
    renderRunner(FINISHED, "print(1)", { language: "python" });
    fireEvent.click(runButton());
    const dialog = await screen.findByTestId("code-run-dialog");
    expect(within(dialog).getByTestId("code-run-dialog-code").textContent).toBe("print(1)");
    expect(within(dialog).queryByTestId("code-run-dialog-commands")).toBeNull();
  });

  it("starts nothing when cancelled", async () => {
    renderRunner();
    fireEvent.click(runButton());
    fireEvent.click(
      within(await screen.findByTestId("code-run-dialog")).getByRole("button", { name: "Cancel" }),
    );
    await waitFor(() => expect(screen.queryByTestId("code-run-dialog")).toBeNull());
    expect(startCodeRun).not.toHaveBeenCalled();
  });

  it("renders as a bottom sheet on narrow screens", async () => {
    vi.mocked(useIsMobileViewport).mockReturnValue(true);
    renderRunner();
    fireEvent.click(runButton());
    expect((await screen.findByTestId("code-run-dialog")).dataset.variant).toBe("sheet");
  });

  it("reopens with the policy's reason and resends marked approved", async () => {
    confirmCodeRuns(SESSION);
    vi.mocked(startCodeRun)
      .mockResolvedValueOnce({ ok: false, code: "needs_approval", reason: "Network access" })
      .mockResolvedValueOnce({ ok: true, runId: "run_new" });
    renderRunner();
    fireEvent.click(runButton());
    const dialog = await screen.findByTestId("code-run-dialog");
    expect(within(dialog).getByText(/Network access/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Run" }));
    await waitFor(() => expect(startCodeRun).toHaveBeenCalledTimes(2));
    expect(vi.mocked(startCodeRun).mock.calls[1]![1].approved).toBe(true);
  });

  it("doesn't reopen for approval once the sandbox started launching mid-request", async () => {
    confirmCodeRuns(SESSION);
    let reply!: (value: Awaited<ReturnType<typeof startCodeRun>>) => void;
    vi.mocked(startCodeRun).mockReturnValueOnce(
      new Promise((resolve) => {
        reply = resolve;
      }),
    );
    renderRunner();
    fireEvent.click(runButton());
    act(() => useChatStore.setState({ sandboxStatus: { stage: "provisioning", error: null } }));
    await act(async () => reply({ ok: false, code: "needs_approval", reason: "Network access" }));
    act(() => useChatStore.setState({ sandboxStatus: null }));
    expect(screen.queryByTestId("code-run-dialog")).toBeNull();
  });

  it("shows a cancelled approval as refused on this device only", async () => {
    confirmCodeRuns(SESSION);
    vi.mocked(startCodeRun).mockResolvedValue({
      ok: false,
      code: "needs_approval",
      reason: "Network access",
    });
    renderRunner();
    fireEvent.click(runButton());
    fireEvent.click(
      within(await screen.findByTestId("code-run-dialog")).getByRole("button", { name: "Cancel" }),
    );
    expect(await screen.findByTestId("code-run-status")).toHaveTextContent(
      "Refused: Network access (not approved)",
    );
  });

  it("shows a policy denial as refused with its reason", async () => {
    confirmCodeRuns(SESSION);
    vi.mocked(startCodeRun).mockResolvedValue({
      ok: false,
      code: "policy_denied",
      reason: "No rm",
    });
    renderRunner();
    fireEvent.click(runButton());
    expect(await screen.findByTestId("code-run-status")).toHaveTextContent("Refused: No rm");
  });
});

describe("output panel", () => {
  it("streams output with stderr apart, elapsed time, and Stop", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
    vi.setSystemTime(new Date(103_000));
    act(() => {
      useCodeRunStore.getState().started(SESSION, startedItem("run_1", 100));
      useCodeRunStore.getState().output(SESSION, "run_1", 0, "stdout", "\u001b[32mok\u001b[0m\n");
      useCodeRunStore.getState().output(SESSION, "run_1", 12, "stderr", "warn\n");
    });
    renderRunner();
    expect(screen.getByTestId("code-run-status")).toHaveTextContent("Running · 3.0s");
    act(() => vi.advanceTimersByTime(2000));
    expect(screen.getByTestId("code-run-status")).toHaveTextContent("Running · 5.0s");
    const panel = screen.getByTestId("code-run-panel");
    expect(within(panel).getByText("warn").closest("pre")!.className).toContain(
      "bg-destructive/10",
    );
    fireEvent.click(screen.getByTestId("code-run-stop"));
    expect(stopCodeRun).toHaveBeenCalledWith(SESSION, "run_1");
  });

  it("shows the exit, the cut note, and earlier runs under a toggle", () => {
    act(() => {
      useCodeRunStore
        .getState()
        .ingestItems(SESSION, [
          startedItem("run_1", 100),
          finishedItem("run_1", { output: [{ stream: "stdout", text: "first\n" }] }),
          startedItem("run_2", 200),
          finishedItem("run_2", { truncated: true, exit_code: 2 }),
        ]);
    });
    renderRunner();
    expect(screen.getByTestId("code-run-status")).toHaveTextContent("Exited with code 2 · 1.5s");
    expect(screen.getByText(/Output cut/)).toBeInTheDocument();
    expect(screen.queryByText("first")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show 1 earlier run" }));
    expect(screen.getByText("first")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide earlier runs" })).toBeInTheDocument();
  });

  it("notes the cut while a run streams past the saved-output cap", () => {
    act(() => {
      useCodeRunStore.getState().started(SESSION, startedItem("run_1", 100));
      useCodeRunStore.getState().output(SESSION, "run_1", 0, "stdout", "z".repeat(100_001));
    });
    renderRunner();
    expect(screen.getByTestId("code-run-status")).toHaveTextContent("Running");
    expect(screen.getByText(/Output cut/)).toBeInTheDocument();
  });

  describe("expand control", () => {
    // jsdom has no layout: stand in a content height and fire resize callbacks.
    let contentHeight = 0;
    let observers: { callback: () => void; target: Element | null }[] = [];
    const resize = () => act(() => observers.forEach(({ callback }) => callback()));

    beforeEach(() => {
      contentHeight = 0;
      observers = [];
      vi.stubGlobal(
        "ResizeObserver",
        class {
          entry: { callback: () => void; target: Element | null };
          constructor(callback: () => void) {
            this.entry = { callback, target: null };
            observers.push(this.entry);
          }
          observe(target: Element) {
            this.entry.target = target;
            queueMicrotask(() => this.entry.target && this.entry.callback());
          }
          disconnect() {
            this.entry.target = null;
            observers = observers.filter((entry) => entry !== this.entry);
          }
          unobserve() {}
        },
      );
      vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (
        this: HTMLElement,
      ) {
        return this.dataset.testid === "code-run-output" ? contentHeight : 0;
      });
      vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (
        this: HTMLElement,
      ) {
        if (this.dataset.testid !== "code-run-output") return 0;
        return this.classList.contains("max-h-72") ? Math.min(contentHeight, 288) : contentHeight;
      });
    });

    afterEach(() => {
      vi.unstubAllGlobals();
      vi.restoreAllMocks();
    });

    it("is offered only when the output is taller than the cap", async () => {
      contentHeight = 40;
      act(() => {
        useCodeRunStore
          .getState()
          .ingestItems(SESSION, [startedItem("run_1", 100), finishedItem("run_1")]);
      });
      renderRunner();
      await act(async () => {});
      expect(screen.getByTestId("code-run-output").className).toContain("max-h-72");
      expect(screen.queryByRole("button", { name: "Expand output" })).toBeNull();
    });

    it("lifts the cap and restores it, and stays expanded as output streams", async () => {
      contentHeight = 600;
      act(() => {
        useCodeRunStore.getState().started(SESSION, startedItem("run_1", 100));
        useCodeRunStore.getState().output(SESSION, "run_1", 0, "stdout", "line\n".repeat(40));
      });
      renderRunner();
      await act(async () => {});
      const output = screen.getByTestId("code-run-output");
      fireEvent.click(screen.getByRole("button", { name: "Expand output" }));
      expect(output.className).not.toContain("max-h-72");

      contentHeight = 900;
      act(() => {
        useCodeRunStore.getState().output(SESSION, "run_1", 200, "stdout", "more\n".repeat(20));
      });
      resize();
      expect(screen.getByTestId("code-run-output")).toBe(output);
      expect(output.className).not.toContain("max-h-72");
      // The run ends while expanded; collapsing must still offer Expand again.
      act(() => useCodeRunStore.getState().finished(SESSION, finishedItem("run_1")));
      await act(async () => {});
      fireEvent.click(screen.getByRole("button", { name: "Collapse output" }));
      expect(output.className).toContain("max-h-72");
      await act(async () => {});
      expect(screen.getByRole("button", { name: "Expand output" })).toBeInTheDocument();
    });

    it("appears once streamed output grows past the cap", async () => {
      contentHeight = 40;
      act(() => {
        useCodeRunStore.getState().started(SESSION, startedItem("run_1", 100));
        useCodeRunStore.getState().output(SESSION, "run_1", 0, "stdout", "a\n");
      });
      renderRunner();
      await act(async () => {});
      expect(screen.queryByRole("button", { name: "Expand output" })).toBeNull();
      contentHeight = 600;
      act(() => {
        useCodeRunStore.getState().output(SESSION, "run_1", 2, "stdout", "b\n".repeat(300));
      });
      resize();
      expect(screen.getByRole("button", { name: "Expand output" })).toBeInTheDocument();
    });

    it("starts collapsed for a new run of the same block", async () => {
      contentHeight = 600;
      act(() => {
        useCodeRunStore
          .getState()
          .ingestItems(SESSION, [startedItem("run_1", 100), finishedItem("run_1")]);
      });
      renderRunner();
      await act(async () => {});
      fireEvent.click(screen.getByRole("button", { name: "Expand output" }));
      act(() => {
        useCodeRunStore
          .getState()
          .ingestItems(SESSION, [startedItem("run_2", 200), finishedItem("run_2")]);
      });
      await act(async () => {});
      expect(screen.getAllByTestId("code-run-output")[0]!.className).toContain("max-h-72");
    });
  });

  it.each([
    [true, "Interrupted"],
    [false, "Waiting for the runner"],
  ])("labels an unlisted unfinished run (runner online %s) as %s", (online, label) => {
    setSession({ runnerOnline: online });
    act(() => {
      useCodeRunStore.getState().ingestItems(SESSION, [startedItem("run_1", 100)]);
      useCodeRunStore.getState().listingSettled(SESSION);
    });
    renderRunner();
    expect(screen.getByTestId("code-run-status")).toHaveTextContent(label);
    expect(screen.queryByTestId("code-run-stop")).toBeNull();
  });

  it("shows viewers the panel without Run or Stop", () => {
    vi.mocked(useIsOwner).mockReturnValue(false);
    vi.mocked(useCanEdit).mockReturnValue(false);
    act(() => {
      useCodeRunStore.getState().started(SESSION, startedItem("run_1", 100));
      useCodeRunStore.getState().output(SESSION, "run_1", 0, "stdout", "live\n");
    });
    renderRunner();
    expect(screen.getByTestId("code-run-panel")).toHaveTextContent("live");
    expect(screen.queryByTestId("code-run-button")).toBeNull();
    expect(screen.queryByTestId("code-run-stop")).toBeNull();
  });
});

describe("harnesses that don't receive runs", () => {
  it("note it and add the output to the composer as a fenced block", () => {
    setSession({ harness: "cursor-native" });
    act(() => {
      useCodeRunStore
        .getState()
        .ingestItems(SESSION, [startedItem("run_1", 100), finishedItem("run_1")]);
    });
    renderRunner();
    expect(screen.getByText("This agent doesn't receive runs automatically")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add output to message" }));
    expect(useChatStore.getState().pendingComposerText).toEqual([
      "```text\nCommand (shell):\necho hi\n\nOutput:\nhi\n\nExited with code 0\n```",
    ]);
  });

  it("say nothing for a harness that receives runs", () => {
    act(() => {
      useCodeRunStore
        .getState()
        .ingestItems(SESSION, [startedItem("run_1", 100), finishedItem("run_1")]);
    });
    renderRunner();
    expect(screen.queryByText("This agent doesn't receive runs automatically")).toBeNull();
  });
});
