// Performance bounds for code run panels: what one output batch re-renders,
// the cost of streaming to the output cap, block hashing, and hydration.
import { act, cleanup, render } from "@testing-library/react";
import { Profiler, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useCanEdit, useIsOwner } from "@/hooks/usePermissions";
import { useSession } from "@/hooks/useSession";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { sha256Hex } from "@/lib/codeRuns";
import { useCodeRunStore } from "@/store/codeRunStore";
import { ConversationScopeContext } from "./conversationScope";
import { CodeBlockRunner } from "./CodeRunPanel";

vi.mock("@/hooks/useSession", () => ({ useSession: vi.fn() }));
vi.mock("@/hooks/usePermissions", () => ({ useIsOwner: vi.fn(), useCanEdit: vi.fn() }));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: vi.fn() }));
// Characters handed to the ANSI parser, delegating to the real component.
const ansiParsed = vi.hoisted(() => ({ chars: 0 }));
vi.mock("ansi-to-react", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  const module = actual.default as Record<string, unknown> | ((props: unknown) => unknown);
  const Real = (typeof module === "function" ? module : module.default) as (props: {
    children?: string;
  }) => unknown;
  const Counting = (props: { children?: string }) => {
    ansiParsed.chars += props.children?.length ?? 0;
    return Real(props);
  };
  return { default: Counting };
});
vi.mock("@/lib/codeRuns", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, sha256Hex: vi.fn(actual.sha256Hex as (text: string) => string) };
});

const SESSION = "conv_perf";
const MESSAGE = { messageId: "msg_1", streaming: false };
const REPORT = process.env.CODE_RUN_PERF_REPORT === "1";
const report = (label: string, value: unknown) => {
  if (REPORT) process.stderr.write(`[code-run-perf] ${label}: ${JSON.stringify(value)}\n`);
};

function started(runId: string, code: string, startedAt = 100) {
  return {
    type: "code_run",
    run_id: runId,
    phase: "started",
    message_id: "msg_1",
    block_hash: sha256Hex(code),
    language: "shell",
    command: code,
    started_at: startedAt,
  };
}

function Block({ code, onRender }: { code: string; onRender: () => void }) {
  return (
    <Profiler id={code} onRender={onRender}>
      <CodeBlockRunner
        message={MESSAGE}
        code={code}
        language="shell"
        console={false}
        render={(button?: ReactNode) => <div>{button}</div>}
      />
    </Profiler>
  );
}

beforeEach(() => {
  useCodeRunStore.setState({ sessions: {} });
  vi.mocked(useSession).mockReturnValue({
    session: { runnerId: "runner_1", runnerOnline: true, harness: "openai-agents" },
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof useSession>);
  vi.mocked(useIsOwner).mockReturnValue(true);
  vi.mocked(useCanEdit).mockReturnValue(true);
  vi.mocked(useIsMobileViewport).mockReturnValue(false);
});

afterEach(cleanup);

describe("code run panel performance", () => {
  it("re-renders only the block whose run got output", () => {
    const renders = { a: 0, b: 0 };
    act(() => {
      useCodeRunStore
        .getState()
        .ingestItems(SESSION, [started("run_a", "$ echo a"), started("run_b", "$ echo b")]);
    });
    render(
      <ConversationScopeContext.Provider value={SESSION}>
        <Block code="$ echo a" onRender={() => renders.a++} />
        <Block code="$ echo b" onRender={() => renders.b++} />
      </ConversationScopeContext.Provider>,
    );
    const before = { ...renders };
    for (let batch = 0; batch < 10; batch++) {
      act(() => useCodeRunStore.getState().output(SESSION, "run_a", batch * 4, "stdout", "out\n"));
    }
    const delta = { a: renders.a - before.a, b: renders.b - before.b };
    report("renders per 10 batches to block a", delta);
    expect(delta.b).toBe(0);
    expect(delta.a).toBeGreaterThan(0);
  });

  it("streams to the 100,000 character cap in 500 batches without quadratic work", () => {
    act(() => useCodeRunStore.getState().ingestItems(SESSION, [started("run_a", "$ seq")]));
    const { container } = render(
      <ConversationScopeContext.Provider value={SESSION}>
        <Block code="$ seq" onRender={() => {}} />
      </ConversationScopeContext.Provider>,
    );
    // 200 characters a batch: five colored 40-character lines, 100,000 in all.
    const line = "\u001b[32m" + "x".repeat(25) + "\u001b[0m line\n";
    const batch = line.repeat(5);
    const timings: number[] = [];
    let offset = 0;
    ansiParsed.chars = 0;
    const startedAt = performance.now();
    for (let index = 0; index < 500; index++) {
      const t0 = performance.now();
      act(() => useCodeRunStore.getState().output(SESSION, "run_a", offset, "stdout", batch));
      timings.push(performance.now() - t0);
      offset += batch.length;
    }
    const total = performance.now() - startedAt;
    const first50 = timings.slice(0, 50).reduce((sum, t) => sum + t, 0) / 50;
    const last50 = timings.slice(-50).reduce((sum, t) => sum + t, 0) / 50;
    report("stream 500 batches", {
      totalMs: Math.round(total),
      first50AvgMs: +first50.toFixed(2),
      last50AvgMs: +last50.toFixed(2),
      chars: offset,
      parsedChars: ansiParsed.chars,
    });
    expect(offset).toBe(100_000);
    expect(container.textContent).toContain("x".repeat(25));
    // Deterministic: each batch re-parses only the open piece (at most about
    // 4 KB), so the whole stream parses a few times its size. Re-parsing all
    // output per batch would be about 25,000,000 characters.
    expect(ansiParsed.chars).toBeLessThan(2_500_000);
    // Relative only: linear work keeps the last batches about as cheap as the first.
    expect(last50).toBeLessThan(first50 * 4);
  });

  it("hashes a block once however often it renders", () => {
    vi.mocked(sha256Hex).mockClear();
    const view = render(
      <ConversationScopeContext.Provider value={SESSION}>
        <Block code="$ echo once" onRender={() => {}} />
      </ConversationScopeContext.Provider>,
    );
    for (let index = 0; index < 50; index++) {
      view.rerender(
        <ConversationScopeContext.Provider value={SESSION}>
          <Block code="$ echo once" onRender={() => {}} />
        </ConversationScopeContext.Provider>,
      );
    }
    view.unmount();
    for (let index = 0; index < 10; index++) {
      render(
        <ConversationScopeContext.Provider value={SESSION}>
          <Block code="$ echo once" onRender={() => {}} />
        </ConversationScopeContext.Provider>,
      ).unmount();
    }
    const calls = vi.mocked(sha256Hex).mock.calls.length;
    report("sha256 calls for 50 rerenders + 10 remounts", calls);
    expect(calls).toBe(1);
  });

  it("hydrates a page of saved runs in one store update", () => {
    const items = Array.from({ length: 500 }, (_, index) => [
      started(`run_${index}`, `$ echo ${index}`, index),
      {
        type: "code_run",
        run_id: `run_${index}`,
        phase: "finished",
        status: "exited",
        exit_code: 0,
        duration_ms: 1,
        output: [{ stream: "stdout", text: "ok\n" }],
      },
    ]).flat();
    let updates = 0;
    const unsubscribe = useCodeRunStore.subscribe(() => updates++);
    const t0 = performance.now();
    useCodeRunStore.getState().ingestItems(SESSION, items);
    const elapsed = performance.now() - t0;
    unsubscribe();
    report("hydrate 1000 items", { updates, ms: +elapsed.toFixed(1) });
    expect(Object.keys(useCodeRunStore.getState().sessions[SESSION]!.runs)).toHaveLength(500);
    expect(updates).toBe(1);
  });

  it("runs the elapsed-time ticker only while a run is active", () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    try {
      act(() => {
        useCodeRunStore.getState().ingestItems(SESSION, [
          started("run_a", "$ echo t"),
          {
            type: "code_run",
            run_id: "run_a",
            phase: "finished",
            status: "exited",
            exit_code: 0,
            duration_ms: 1,
            output: [],
          },
          started("run_b", "$ echo u"),
        ]);
      });
      const view = render(
        <ConversationScopeContext.Provider value={SESSION}>
          <Block code="$ echo t" onRender={() => {}} />
        </ConversationScopeContext.Provider>,
      );
      const finishedTimers = vi.getTimerCount();
      view.rerender(
        <ConversationScopeContext.Provider value={SESSION}>
          <Block code="$ echo t" onRender={() => {}} />
          <Block code="$ echo u" onRender={() => {}} />
        </ConversationScopeContext.Provider>,
      );
      const withRunning = vi.getTimerCount();
      act(() =>
        useCodeRunStore.getState().finished(SESSION, {
          type: "code_run",
          run_id: "run_b",
          phase: "finished",
          status: "exited",
          exit_code: 0,
          duration_ms: 1,
          output: [],
        }),
      );
      const afterFinish = vi.getTimerCount();
      report("timers finished/running/after finish", [finishedTimers, withRunning, afterFinish]);
      expect([finishedTimers, withRunning, afterFinish]).toEqual([0, 1, 0]);
    } finally {
      vi.useRealTimers();
    }
  });
});
