import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { START_TIMED_OUT } from "./terminalTabConnection";
import { TerminalTabStatus, VIEWER_RESUME_HINT } from "./TerminalTabStatus";

afterEach(cleanup);

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
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Starting the session took too long.");
    expect(screen.getByRole("button", { name: "Try Again" })).toBeInTheDocument();
  });

  it("records why a shell tab stalled, once per reason, for bug reports", () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    const view = render(
      <TerminalTabStatus
        connection={{ kind: "connecting" }}
        sandboxNotice={null}
        onTryAgain={vi.fn()}
      />,
    );
    expect(info).not.toHaveBeenCalled();
    view.rerender(
      <TerminalTabStatus
        connection={{ kind: "unreachable" }}
        sandboxNotice={null}
        onTryAgain={vi.fn()}
      />,
    );
    view.rerender(
      <TerminalTabStatus
        connection={{ kind: "unreachable" }}
        sandboxNotice={null}
        onTryAgain={vi.fn()}
      />,
    );
    expect(info).toHaveBeenCalledTimes(1);
    expect(info).toHaveBeenCalledWith("[terminal] connect stalled: reason=unreachable");
    info.mockRestore();
  });
});
