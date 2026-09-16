import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConnectionState } from "@/components/blocks/TerminalSession";
import type { SetupOperation } from "@/lib/providerSetupApi";
import { ProviderSetupTerminal } from "./ProviderSetupTerminal";

const sessions: {
  onState: (state: ConnectionState) => void;
  dispose: ReturnType<typeof vi.fn>;
  setTheme: ReturnType<typeof vi.fn>;
}[] = [];

vi.mock("@/components/theme/useResolvedThemeMode", () => ({
  useResolvedThemeMode: () => "light",
}));

vi.mock("@/components/blocks/TerminalSession", () => ({
  TerminalSession: class {
    dispose = vi.fn();
    setTheme = vi.fn();

    constructor(_node: HTMLElement, _url: string, onState: (state: ConnectionState) => void) {
      sessions.push({ onState, dispose: this.dispose, setTheme: this.setTheme });
    }
  },
}));

vi.mock("@/lib/providerSetupApi", () => ({
  setupOperationAttachUrl: vi.fn(() => "ws://example.test/attach"),
  fetchSetupOperation: vi.fn(),
  cancelSetupOperation: vi.fn(),
  verifySetupOperation: vi.fn(),
}));

import * as providerSetupApi from "@/lib/providerSetupApi";

const runningOperation: SetupOperation = {
  operation_id: "op_123",
  state: "running",
  action: "claude-login",
  exit_code: null,
  error: null,
};

afterEach(() => {
  cleanup();
  sessions.splice(0, sessions.length);
  vi.clearAllMocks();
});

describe("ProviderSetupTerminal", () => {
  it("shows a running setup operation and cancels through the operation API", async () => {
    const onOperationChange = vi.fn();
    const onFinished = vi.fn();
    const cancelled = { ...runningOperation, state: "cancelled" as const };
    vi.mocked(providerSetupApi.cancelSetupOperation).mockResolvedValue(cancelled);

    render(
      <ProviderSetupTerminal
        hostId="host_a"
        operation={runningOperation}
        onOperationChange={onOperationChange}
        onFinished={onFinished}
      />,
    );

    expect(screen.getByText("Connecting terminal…")).toHaveClass("z-20");
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText(/Complete the requested step/i)).toBeInTheDocument();
    expect(screen.getByText("Sign-in help")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(providerSetupApi.cancelSetupOperation).toHaveBeenCalledWith("host_a", "op_123"),
    );
    expect(onOperationChange).toHaveBeenCalledWith(cancelled);
    expect(onFinished).toHaveBeenCalledOnce();
  });

  it("offers a fresh attach when the terminal bridge closes and disposes it on unmount", async () => {
    const view = render(
      <ProviderSetupTerminal
        hostId="host_a"
        operation={runningOperation}
        onOperationChange={vi.fn()}
        onFinished={vi.fn()}
      />,
    );

    await waitFor(() => expect(sessions).toHaveLength(1));
    act(() => sessions[0].onState({ kind: "closed", code: 1006, reason: "network dropped" }));

    expect(screen.getByText(/Terminal bridge closed: network dropped/i)).toBeInTheDocument();
    const retryButton = screen.getByRole("button", { name: "Retry terminal" });
    expect(retryButton.parentElement).toHaveClass("z-20");
    fireEvent.click(retryButton);

    await waitFor(() => expect(sessions).toHaveLength(2));
    expect(sessions[0].dispose).toHaveBeenCalledOnce();

    view.unmount();
    expect(sessions[1].dispose).toHaveBeenCalledOnce();
  });

  it("shows a persistence failure without describing an exit-zero command as successful", () => {
    render(
      <ProviderSetupTerminal
        hostId="host_a"
        operation={{
          ...runningOperation,
          state: "failed",
          exit_code: 0,
          error: "Login finished but the provider could not be saved.",
        }}
        onOperationChange={vi.fn()}
        onFinished={vi.fn()}
      />,
    );

    expect(screen.getByText("The setup command did not complete.")).toBeInTheDocument();
    expect(
      screen.getByText("Login finished but the provider could not be saved."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/success/i)).toBeNull();
  });

  it("keeps completed output visible without offering a retry after the bridge closes", async () => {
    const onFinished = vi.fn();
    render(
      <ProviderSetupTerminal
        hostId="host_a"
        operation={{ ...runningOperation, state: "succeeded", exit_code: 0 }}
        onOperationChange={vi.fn()}
        onFinished={onFinished}
      />,
    );

    expect(screen.getByText("Setup command completed.")).toBeInTheDocument();
    expect(sessions).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "View output" })).toBeNull();

    expect(screen.queryByRole("button", { name: "Retry terminal" })).toBeNull();
    expect(onFinished).toHaveBeenCalledOnce();
  });

  it("keeps the existing terminal session when completed output is collapsed and reopened", async () => {
    const onOperationChange = vi.fn();
    const view = render(
      <ProviderSetupTerminal
        hostId="host_a"
        operation={runningOperation}
        onOperationChange={onOperationChange}
        onFinished={vi.fn()}
      />,
    );
    await waitFor(() => expect(sessions).toHaveLength(1));

    view.rerender(
      <ProviderSetupTerminal
        hostId="host_a"
        operation={{ ...runningOperation, state: "succeeded", exit_code: 0 }}
        onOperationChange={onOperationChange}
        onFinished={vi.fn()}
      />,
    );

    expect(await screen.findByText("Command output is hidden.")).toBeInTheDocument();
    expect(sessions[0].dispose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "View output" }));
    expect(sessions).toHaveLength(1);
  });

  it("checks an Antigravity connection only when the operation allows verification", async () => {
    const onOperationChange = vi.fn();
    const verified = {
      ...runningOperation,
      action: "antigravity-login" as const,
      state: "succeeded" as const,
      exit_code: 0,
    };
    vi.mocked(providerSetupApi.verifySetupOperation).mockResolvedValue(verified);

    render(
      <ProviderSetupTerminal
        hostId="host_a"
        operation={{ ...runningOperation, action: "antigravity-login", can_verify: true }}
        onOperationChange={onOperationChange}
        onFinished={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Check connection" }));

    await waitFor(() =>
      expect(providerSetupApi.verifySetupOperation).toHaveBeenCalledWith("host_a", "op_123"),
    );
    expect(onOperationChange).toHaveBeenCalledWith(verified);
  });

  it("keeps a rejected connection check visible while setup continues", async () => {
    vi.mocked(providerSetupApi.verifySetupOperation).mockRejectedValue(
      Object.assign(new Error("Not connected"), { status: 409 }),
    );

    render(
      <ProviderSetupTerminal
        hostId="host_a"
        operation={{ ...runningOperation, action: "antigravity-login", can_verify: true }}
        onOperationChange={vi.fn()}
        onFinished={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Check connection" }));

    expect(
      await screen.findByText("Connection not detected yet. Finish signing in, then check again."),
    ).toBeInTheDocument();
  });
});
