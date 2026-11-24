import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AGENT_RECONNECT_TIMEOUT_MS, MissingAgentPane } from "./MissingAgentPane";

vi.mock("@/hooks/RunnerHealthProvider", () => ({ useSessionHostOnline: () => true }));

function pane(props: Partial<Parameters<typeof MissingAgentPane>[0]> = {}) {
  return (
    <MissingAgentPane
      conversationId="conv_missing_agent"
      onResume={vi.fn(async () => {})}
      visible
      readOnly={false}
      listSettled
      allowance={{ current: { attempted: false } }}
      {...props}
    />
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("MissingAgentPane", () => {
  it("says the terminal didn't reconnect when a resumed session never lists it", async () => {
    vi.useFakeTimers();
    render(pane());
    expect(screen.getByRole("status")).toHaveTextContent("Reconnecting…");
    await act(async () => {});
    await act(async () => vi.advanceTimersByTime(AGENT_RECONNECT_TIMEOUT_MS));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The terminal didn't reconnect. Try resuming the session.",
    );
    expect(screen.getByRole("button", { name: "Resume session" })).toBeEnabled();
  });

  it("waits for Resume session once this opening already recovered", () => {
    const onResume = vi.fn(async () => {});
    render(pane({ onResume, allowance: { current: { attempted: true } } }));
    expect(onResume).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Resume session" })).toBeEnabled();
  });

  it("says the terminal is unavailable where the viewer can't resume the session", () => {
    render(pane({ onResume: undefined }));
    expect(screen.getByText("Terminal unavailable.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
