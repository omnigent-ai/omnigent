import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("shows the error and its existing recovery action on separate lines", async () => {
    const errorMessage = "Sign-in to https://team.example.com/ didn't complete.";
    const errorAction = "Finish signing in and try again.";
    const onRun = vi.fn().mockResolvedValue({
      ok: false,
      error: `${errorMessage} ${errorAction}`,
      errorMessage,
      errorAction,
    });
    render(<SetupTerminalStep onRun={onRun} onBack={vi.fn()} />);

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(errorMessage)).toHaveClass("text-destructive");
    expect(screen.queryByText(`${errorMessage} ${errorAction}`)).toBeNull();
    const actionLine = screen.getByText(errorAction);
    // The next step is guidance, so it must not inherit the failure's red.
    expect(actionLine).not.toHaveClass("text-destructive");
    expect(actionLine.className).not.toMatch(/cyan|red/);
    expect(actionLine.querySelector("span")).toHaveAttribute("aria-hidden", "true");
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

  it("fills the bar from the start when there's no install step", () => {
    let emit: (line: string) => void = () => {};
    const props = {
      onRun: () => new Promise<{ ok: boolean }>(() => {}),
      onSetupLog: (cb: (line: string) => void) => {
        emit = cb;
        return () => {};
      },
      onBack: vi.fn(),
    };
    const { container, rerender } = render(<SetupTerminalStep {...props} />);
    const bar = () => container.querySelector<HTMLElement>("[style*='width']")?.style.width;
    expect(bar()).toBe("10%");
    act(() => emit("$ omnigent host --server https://team.example.com/"));
    expect(bar()).toBe("35%");
    rerender(<SetupTerminalStep {...props} connection={{ phase: "connecting" }} />);
    expect(bar()).toBe("70%");
  });

  it("fires onBack from Back on the failure screen", async () => {
    const onBack = vi.fn();
    render(<SetupTerminalStep onRun={vi.fn().mockResolvedValue({ ok: false })} onBack={onBack} />);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(onBack).toHaveBeenCalledOnce();
  });

  describe("copy diagnostics", () => {
    const failure = {
      ok: false,
      error:
        "Sign-in to https://srv.example.com/ didn't complete. Finish signing in and try again.",
      errorMessage: "Sign-in to https://srv.example.com/ didn't complete.",
      errorAction: "Finish signing in and try again.",
      errorKind: "laptop-sign-in",
    };

    it("copies only curated fields and reports Copied after the write resolves", async () => {
      const onCopyDiagnostics = vi.fn().mockResolvedValue(undefined);
      render(
        <SetupTerminalStep
          onRun={vi.fn().mockResolvedValue(failure)}
          onBack={vi.fn()}
          onCopyDiagnostics={onCopyDiagnostics}
          diagnosticsContext={{ runner: "laptop", serverUrl: "https://srv.example.com/a?ticket=T" }}
        />,
      );
      fireEvent.click(await screen.findByRole("button", { name: /Copy diagnostics/ }));
      expect(await screen.findByText("Copied")).toBeInTheDocument();
      const text = onCopyDiagnostics.mock.calls[0][0] as string;
      expect(text).toContain("Failure kind: laptop-sign-in");
      expect(text).toContain("Runner: laptop");
      expect(text).toContain("Stage: connect");
      expect(text).toContain("Server: https://srv.example.com\n");
      expect(text).toContain("Next step: Finish signing in and try again.");
      expect(text).not.toContain("ticket");
    });

    it("shows a failure, not Copied, when the clipboard write rejects or returns false", async () => {
      const onCopyDiagnostics = vi
        .fn()
        .mockRejectedValueOnce(new Error("denied"))
        .mockResolvedValueOnce(false)
        .mockResolvedValueOnce(undefined);
      render(
        <SetupTerminalStep
          onRun={vi.fn().mockResolvedValue(failure)}
          onBack={vi.fn()}
          onCopyDiagnostics={onCopyDiagnostics}
        />,
      );
      const button = await screen.findByRole("button", { name: /Copy diagnostics/ });
      fireEvent.click(button);
      expect(await screen.findByText(/Couldn't copy/)).toBeInTheDocument();
      expect(screen.queryByText("Copied")).toBeNull();
      fireEvent.click(button);
      await waitFor(() => expect(onCopyDiagnostics).toHaveBeenCalledTimes(2));
      expect(await screen.findByText(/Couldn't copy/)).toBeInTheDocument();
      fireEvent.click(button);
      expect(await screen.findByText("Copied")).toBeInTheDocument();
    });

    it("withholds legacy combined error text and labels simulated runs", async () => {
      const onCopyDiagnostics = vi.fn().mockResolvedValue(undefined);
      render(
        <SetupTerminalStep
          onRun={vi.fn().mockResolvedValue({
            ok: false,
            error: "Failed: /home/alice/.ssh/id_rsa permission denied",
          })}
          onBack={vi.fn()}
          onCopyDiagnostics={onCopyDiagnostics}
          diagnosticsContext={{ simulated: true }}
        />,
      );
      // The legacy line is still shown to the user as before.
      expect(await screen.findByText(/permission denied/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: /Copy diagnostics/ }));
      await screen.findByText("Copied");
      const text = onCopyDiagnostics.mock.calls[0][0] as string;
      expect(text).toContain("Failure: details withheld");
      expect(text).toContain("Simulated: yes");
      expect(text).not.toContain("alice");
    });

    it("clears the copy status when Retry starts a new run, and hides the button when unsupported", async () => {
      const onRun = vi.fn().mockResolvedValueOnce(failure).mockResolvedValueOnce(failure);
      const { unmount } = render(
        <SetupTerminalStep
          onRun={onRun}
          onBack={vi.fn()}
          onCopyDiagnostics={vi.fn().mockResolvedValue(undefined)}
        />,
      );
      fireEvent.click(await screen.findByRole("button", { name: /Copy diagnostics/ }));
      await screen.findByText("Copied");
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await waitFor(() => expect(onRun).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(screen.queryByText("Copied")).toBeNull());
      unmount();
      render(<SetupTerminalStep onRun={vi.fn().mockResolvedValue(failure)} onBack={vi.fn()} />);
      await screen.findByRole("alert");
      expect(screen.queryByRole("button", { name: /Copy diagnostics/ })).toBeNull();
    });

    it("labels an install failure with the install stage", async () => {
      const onCopyDiagnostics = vi.fn().mockResolvedValue(undefined);
      render(
        <SetupTerminalStep
          onInstallCli={vi.fn().mockResolvedValue({
            ok: false,
            errorMessage: "The installer exited.",
            errorAction: "Try again.",
          })}
          onRun={vi.fn()}
          onBack={vi.fn()}
          onCopyDiagnostics={onCopyDiagnostics}
        />,
      );
      fireEvent.click(
        await screen.findByRole("button", { name: /Copy diagnostics/ }, { timeout: 3000 }),
      );
      await screen.findByText("Copied");
      expect(onCopyDiagnostics.mock.calls[0][0]).toContain("Stage: install");
    });
  });
});
