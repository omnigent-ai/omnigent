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
    expect(actionLine).toHaveClass("text-cyan-700");
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

  describe("Copy diagnostics", () => {
    const errorMessage = "Sign-in to https://team.example.com/ didn't complete.";
    const errorAction = "Finish signing in and try again.";
    const failure = {
      ok: false,
      error: `${errorMessage} ${errorAction}`,
      errorMessage,
      errorAction,
    };

    it("copies only the displayed failure and action as two separate lines", async () => {
      const onCopy = vi.fn().mockResolvedValue(undefined);
      render(
        <SetupTerminalStep
          onRun={vi.fn().mockResolvedValue({
            ...failure,
            errorMessage: `${errorMessage}\n  `,
            errorAction: ` ${errorAction}\n`,
          })}
          onSetupLog={(cb) => {
            cb("Authorization: Bearer secret-token https://team.example.com/ticket/abc123");
            return () => {};
          }}
          onBack={vi.fn()}
          onCopy={onCopy}
        />,
      );

      fireEvent.click(await screen.findByRole("button", { name: "Copy diagnostics" }));
      expect(onCopy).toHaveBeenCalledOnce();
      expect(onCopy).toHaveBeenCalledWith(`${errorMessage}\n${errorAction}`);
      expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    });

    it("copies the legacy fallback line when only an error string is displayed", async () => {
      const onCopy = vi.fn().mockResolvedValue(undefined);
      const { unmount } = render(
        <SetupTerminalStep
          onRun={vi.fn().mockResolvedValue({ ok: false, error: "omnigent CLI not found" })}
          onBack={vi.fn()}
          onCopy={onCopy}
        />,
      );
      fireEvent.click(await screen.findByRole("button", { name: "Copy diagnostics" }));
      expect(onCopy).toHaveBeenLastCalledWith("omnigent CLI not found");
      unmount();

      render(
        <SetupTerminalStep
          onRun={vi.fn().mockResolvedValue({ ok: false })}
          onBack={vi.fn()}
          onCopy={onCopy}
        />,
      );
      fireEvent.click(await screen.findByRole("button", { name: "Copy diagnostics" }));
      expect(onCopy).toHaveBeenLastCalledWith("Could not set up Omnigent.");
    });

    it("is offered only on a failure, and only when a copy callback is provided", async () => {
      const onCopy = vi.fn().mockResolvedValue(undefined);
      let finish: (result: { ok: boolean }) => void = () => {};
      const { unmount } = render(
        <SetupTerminalStep
          onRun={() =>
            new Promise((resolve) => {
              finish = resolve;
            })
          }
          onBack={vi.fn()}
          onCopy={onCopy}
        />,
      );
      expect(screen.queryByRole("button", { name: /copy diagnostics/i })).toBeNull();
      act(() => finish({ ok: true }));
      expect(await screen.findByText("Server ready")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /copy diagnostics/i })).toBeNull();
      unmount();

      render(<SetupTerminalStep onRun={vi.fn().mockResolvedValue(failure)} onBack={vi.fn()} />);
      expect(await screen.findByText(errorMessage)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /copy diagnostics/i })).toBeNull();
      expect(onCopy).not.toHaveBeenCalled();
    });

    it.each([
      ["rejects", () => Promise.reject(new Error("clipboard denied"))],
      [
        "throws",
        () => {
          throw new Error("bridge missing");
        },
      ],
    ])("reports a copy that %s and allows a retry", async (_label, failingCopy) => {
      const unhandled = vi.fn();
      process.on("unhandledRejection", unhandled);
      try {
        const onCopy = vi.fn().mockImplementationOnce(failingCopy).mockResolvedValue(undefined);
        render(
          <SetupTerminalStep
            onRun={vi.fn().mockResolvedValue(failure)}
            onBack={vi.fn()}
            onCopy={onCopy}
          />,
        );
        fireEvent.click(await screen.findByRole("button", { name: "Copy diagnostics" }));

        expect(await screen.findByRole("status")).toHaveTextContent(
          "Couldn't copy diagnostics. Try again.",
        );
        expect(screen.queryByText("Copied")).toBeNull();

        fireEvent.click(screen.getByRole("button", { name: "Copy diagnostics" }));
        expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
        expect(screen.getByRole("status")).toBeEmptyDOMElement();
        expect(onCopy).toHaveBeenCalledTimes(2);
        await new Promise((resolve) => {
          setTimeout(resolve, 0);
        });
        expect(unhandled).not.toHaveBeenCalled();
      } finally {
        process.off("unhandledRejection", unhandled);
      }
    });

    it("does not show Copied until the copy is confirmed", async () => {
      let confirm: () => void = () => {};
      const onCopy = vi.fn(
        () =>
          new Promise<void>((resolve) => {
            confirm = resolve;
          }),
      );
      render(
        <SetupTerminalStep
          onRun={vi.fn().mockResolvedValue(failure)}
          onBack={vi.fn()}
          onCopy={onCopy}
        />,
      );
      fireEvent.click(await screen.findByRole("button", { name: "Copy diagnostics" }));
      expect(screen.queryByText("Copied")).toBeNull();
      await act(async () => confirm());
      expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
    });

    it("clears the copied state when Retry starts a new setup attempt", async () => {
      const onCopy = vi.fn().mockResolvedValue(undefined);
      const onRun = vi
        .fn()
        .mockResolvedValueOnce(failure)
        .mockResolvedValueOnce({ ok: false, errorMessage: "Second failure." });
      render(<SetupTerminalStep onRun={onRun} onBack={vi.fn()} onCopy={onCopy} />);

      fireEvent.click(await screen.findByRole("button", { name: "Copy diagnostics" }));
      expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      expect(await screen.findByText("Second failure.")).toBeInTheDocument();
      expect(screen.queryByText("Copied")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Copy diagnostics" }));
      expect(onCopy).toHaveBeenLastCalledWith("Second failure.");
    });

    it("ignores a copy that settles after a new attempt started", async () => {
      let confirm: () => void = () => {};
      const onCopy = vi.fn(
        () =>
          new Promise<void>((resolve) => {
            confirm = resolve;
          }),
      );
      const onRun = vi
        .fn()
        .mockResolvedValueOnce(failure)
        .mockResolvedValueOnce({ ok: false, errorMessage: "Second failure." });
      render(<SetupTerminalStep onRun={onRun} onBack={vi.fn()} onCopy={onCopy} />);

      fireEvent.click(await screen.findByRole("button", { name: "Copy diagnostics" }));
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      expect(await screen.findByText("Second failure.")).toBeInTheDocument();
      await act(async () => confirm());
      expect(screen.queryByText("Copied")).toBeNull();
      expect(screen.getByRole("button", { name: "Copy diagnostics" })).toBeInTheDocument();
    });
  });
});
