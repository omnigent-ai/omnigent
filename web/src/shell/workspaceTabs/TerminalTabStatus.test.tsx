import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LOCAL_RECONNECT_DESCRIPTION } from "@/shell/ReconnectSessionDialog";

import { START_TIMED_OUT } from "./terminalTabConnection";
import { OWNER_RECONNECT_HINT, TerminalTabStatus, VIEWER_RESUME_HINT } from "./TerminalTabStatus";

afterEach(cleanup);

const RECONNECT_COMMAND = "omnigent run path/to/agent.yaml --resume conv_example";

describe("TerminalTabStatus", () => {
  it("shows the sandbox's Resume notice, which resumes it", async () => {
    const resume = vi.fn();
    render(
      <TerminalTabStatus
        connection={{ kind: "sandbox", status: "asleep", failure: null }}
        sandboxNotice={{
          status: "asleep",
          failure: null,
          availability: { available: true },
          resume,
        }}
        onTryAgain={vi.fn()}
        localReconnectCommand={RECONNECT_COMMAND}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Resume/ }));
    expect(resume).toHaveBeenCalledTimes(1);
  });

  it("offers Resume session only to someone who can, and Try Again after a failed start", () => {
    const view = render(
      <TerminalTabStatus
        connection={{ kind: "runner_asleep", failure: null }}
        sandboxNotice={null}
        onTryAgain={vi.fn()}
        localReconnectCommand={RECONNECT_COMMAND}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Session isn't running");
    expect(screen.queryByRole("button")).toBeNull();
    // A viewer isn't told to do what only an editor can.
    expect(screen.getByRole("status")).toHaveTextContent(VIEWER_RESUME_HINT);
    expect(screen.getByRole("status")).not.toHaveTextContent("Resume it to use this terminal.");
    view.rerender(
      <TerminalTabStatus
        connection={{ kind: "runner_asleep", failure: START_TIMED_OUT }}
        sandboxNotice={null}
        onTryAgain={vi.fn()}
        localReconnectCommand={RECONNECT_COMMAND}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(`${START_TIMED_OUT} ${VIEWER_RESUME_HINT}`);
    expect(screen.queryByRole("button")).toBeNull();

    view.rerender(
      <TerminalTabStatus
        connection={{ kind: "runner_asleep", failure: "Starting the session took too long." }}
        sandboxNotice={null}
        onResumeSession={vi.fn()}
        onTryAgain={vi.fn()}
        localReconnectCommand={RECONNECT_COMMAND}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Starting the session took too long.");
    expect(screen.getByRole("button", { name: "Try Again" })).toBeInTheDocument();
  });

  it("tells a stopped session with no host how to relaunch it, and an offline host how to reconnect", () => {
    const view = render(
      <TerminalTabStatus
        connection={{ kind: "local_stranded" }}
        sandboxNotice={null}
        onTryAgain={vi.fn()}
        localReconnectCommand={RECONNECT_COMMAND}
      />,
    );
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Agent disconnected");
    expect(status).toHaveTextContent(LOCAL_RECONNECT_DESCRIPTION);
    expect(status).toHaveTextContent(RECONNECT_COMMAND);
    // There's no host to reconnect, so the tab doesn't point there.
    expect(status).not.toHaveTextContent("Reconnect host");

    // Only the owner can attach a runner; anyone else is told who can.
    view.rerender(
      <TerminalTabStatus
        connection={{ kind: "local_stranded" }}
        sandboxNotice={null}
        onTryAgain={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(OWNER_RECONNECT_HINT);
    expect(screen.getByRole("status")).not.toHaveTextContent(LOCAL_RECONNECT_DESCRIPTION);
    expect(screen.getByRole("status")).not.toHaveTextContent(RECONNECT_COMMAND);

    view.rerender(
      <TerminalTabStatus
        connection={{ kind: "host_offline" }}
        sandboxNotice={null}
        onTryAgain={vi.fn()}
        localReconnectCommand={RECONNECT_COMMAND}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Host is offline");
    expect(screen.getByRole("status")).toHaveTextContent("Reconnect host");
    expect(screen.getByRole("status")).not.toHaveTextContent(RECONNECT_COMMAND);
  });

  it("records why a shell tab stalled, once per reason, for bug reports", () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    const view = render(
      <TerminalTabStatus
        connection={{ kind: "connecting" }}
        sandboxNotice={null}
        onTryAgain={vi.fn()}
        localReconnectCommand={RECONNECT_COMMAND}
      />,
    );
    expect(info).not.toHaveBeenCalled();
    view.rerender(
      <TerminalTabStatus
        connection={{ kind: "unreachable" }}
        sandboxNotice={null}
        onTryAgain={vi.fn()}
        localReconnectCommand={RECONNECT_COMMAND}
      />,
    );
    view.rerender(
      <TerminalTabStatus
        connection={{ kind: "unreachable" }}
        sandboxNotice={null}
        onTryAgain={vi.fn()}
        localReconnectCommand={RECONNECT_COMMAND}
      />,
    );
    expect(info).toHaveBeenCalledTimes(1);
    expect(info).toHaveBeenCalledWith("[terminal] connect stalled: reason=unreachable");
    info.mockRestore();
  });
});
