import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SetupTerminalStep, WARMUP_MS } from "./SetupTerminalStep";

afterEach(cleanup);

describe("SetupTerminalStep", () => {
  it("runs the action on mount and shows Ready on success", async () => {
    const onRun = vi.fn().mockResolvedValue({ ok: true });
    render(<SetupTerminalStep onRun={onRun} onBack={vi.fn()} />);

    expect(onRun).toHaveBeenCalledOnce();
    expect(await screen.findByText("Server ready")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("shows the error and a Retry that runs the action again on failure", async () => {
    const onRun = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, error: "omnigent CLI not found" })
      .mockResolvedValueOnce({ ok: true });
    render(<SetupTerminalStep onRun={onRun} onBack={vi.fn()} />);

    expect(await screen.findByText("omnigent CLI not found")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRun).toHaveBeenCalledTimes(2);
    expect(await screen.findByText("Server ready")).toBeInTheDocument();
  });

  it("installs first when onInstallCli is present, then runs the action", async () => {
    const calls: string[] = [];
    const onInstallCli = vi.fn(async () => {
      calls.push("install");
      return { ok: true };
    });
    const onRun = vi.fn(async () => {
      calls.push("run");
      return { ok: true };
    });
    render(<SetupTerminalStep onInstallCli={onInstallCli} onRun={onRun} onBack={vi.fn()} />);

    expect(await screen.findByText("Server ready")).toBeInTheDocument();
    expect(calls).toEqual(["install", "run"]);
  });

  it("holds the empty terminal for the warm-up beat, then installs", async () => {
    // With a log stream to show, install (and its output) is deferred for the
    // whole beat, so the empty loader/terminal show first.
    vi.useFakeTimers();
    try {
      const onInstallCli = vi.fn().mockResolvedValue({ ok: true });
      const onInstallLog = vi.fn(() => () => {});
      render(
        <SetupTerminalStep
          onInstallCli={onInstallCli}
          onInstallLog={onInstallLog}
          onRun={vi.fn().mockResolvedValue({ ok: true })}
          onBack={vi.fn()}
        />,
      );
      // Still held one tick short of the beat.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(WARMUP_MS - 1);
      });
      expect(onInstallCli).not.toHaveBeenCalled();
      // Fires once the beat elapses.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(onInstallCli).toHaveBeenCalledOnce();
      // The log stream is subscribed before install starts, so no early output
      // is lost during the beat.
      expect(onInstallLog.mock.invocationCallOrder[0]).toBeLessThan(
        onInstallCli.mock.invocationCallOrder[0],
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels the warm-up beat when unmounted during the hold", async () => {
    vi.useFakeTimers();
    try {
      const onInstallCli = vi.fn().mockResolvedValue({ ok: true });
      const onInstallLog = vi.fn(() => () => {});
      const { unmount } = render(
        <SetupTerminalStep
          onInstallCli={onInstallCli}
          onInstallLog={onInstallLog}
          onRun={vi.fn().mockResolvedValue({ ok: true })}
          onBack={vi.fn()}
        />,
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(WARMUP_MS - 1);
      });
      unmount();
      // Past when the beat would have fired: the install must not start.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(WARMUP_MS);
      });
      expect(onInstallCli).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("installs once even when re-rendered with fresh callbacks mid-install", async () => {
    let releaseInstall: ((r: { ok: boolean }) => void) | undefined;
    const installStarts = vi.fn();
    // Stable identity for the assertion, but a NEW wrapper each render (mirrors
    // the parent recreating onInstallCli/onRun) to prove we don't re-fire.
    const installImpl = () => {
      installStarts();
      return new Promise<{ ok: boolean }>((res) => {
        releaseInstall = res;
      });
    };
    const onRun = vi.fn().mockResolvedValue({ ok: true });
    const { rerender } = render(
      <SetupTerminalStep
        onInstallCli={() => installImpl()}
        onRun={() => onRun()}
        onBack={vi.fn()}
      />,
    );
    // A parent re-render (e.g. deferred server list resolved) with new callbacks.
    rerender(
      <SetupTerminalStep
        onInstallCli={() => installImpl()}
        onRun={() => onRun()}
        onBack={vi.fn()}
      />,
    );
    releaseInstall?.({ ok: true });
    expect(await screen.findByText("Server ready")).toBeInTheDocument();
    expect(installStarts).toHaveBeenCalledTimes(1);
    expect(onRun).toHaveBeenCalledTimes(1);
  });

  it("stops at the install failure and does not run the action", async () => {
    const onInstallCli = vi.fn().mockResolvedValue({ ok: false, error: "install broke" });
    const onRun = vi.fn().mockResolvedValue({ ok: true });
    render(<SetupTerminalStep onInstallCli={onInstallCli} onRun={onRun} onBack={vi.fn()} />);

    expect(await screen.findByText("install broke")).toBeInTheDocument();
    expect(onRun).not.toHaveBeenCalled();
  });

  it("renders streamed log lines from onSetupLog and unsubscribes on unmount", async () => {
    const unsubscribe = vi.fn();
    let emit: ((line: string) => void) | undefined;
    const onSetupLog = vi.fn((cb: (line: string) => void) => {
      emit = cb;
      return unsubscribe;
    });
    const onRun = vi.fn().mockResolvedValue({ ok: true });
    const { unmount } = render(
      <SetupTerminalStep onRun={onRun} onSetupLog={onSetupLog} onBack={vi.fn()} />,
    );

    expect(onSetupLog).toHaveBeenCalledOnce();
    emit?.("Starting omnigent server on 127.0.0.1:6767");
    emit?.("Uvicorn running on http://127.0.0.1:6767");
    expect(await screen.findByText("Uvicorn running on http://127.0.0.1:6767")).toBeInTheDocument();
    expect(screen.getByText("Starting omnigent server on 127.0.0.1:6767")).toBeInTheDocument();

    unmount();
    expect(unsubscribe).toHaveBeenCalledOnce();
  });

  it("fires onBack from Back on the failure screen", async () => {
    const onBack = vi.fn();
    render(<SetupTerminalStep onRun={vi.fn().mockResolvedValue({ ok: false })} onBack={onBack} />);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(onBack).toHaveBeenCalledOnce();
  });
});
