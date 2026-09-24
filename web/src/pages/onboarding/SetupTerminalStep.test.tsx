import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SetupTerminalStep } from "./SetupTerminalStep";

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
