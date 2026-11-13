import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { SessionCostButton, SessionOverview } from "./SessionOverview";

beforeEach(() => {
  useChatStore.setState({
    sessionCostUsd: null,
    sessionUsageByModel: null,
    loadingConversation: false,
    sessionUsageLoading: false,
  });
});

afterEach(cleanup);

describe("SessionOverview", () => {
  it.each([
    [{ loadingConversation: true }, null, null],
    [{ sessionUsageLoading: true }, null, null],
    [{}, "Unavailable", "—"],
    [{ sessionCostUsd: 0, sessionUsageLoading: true }, "$0.00", "$0.00"],
  ])("separates loading, unavailable and known costs (%o)", (state, overviewText, chipText) => {
    useChatStore.setState(state);
    render(
      <>
        <SessionOverview onClose={() => {}} />
        <SessionCostButton expanded onToggle={() => {}} />
      </>,
    );

    for (const [element, text] of [
      [screen.getByTestId("session-overview-cost"), overviewText],
      [screen.getByTestId("header-session-cost"), chipText],
    ] as const) {
      if (text === null) expect(within(element).getByLabelText("Loading cost")).toBeTruthy();
      else expect(element).toHaveTextContent(text);
    }
  });

  it("shows the per-model usage and closes from its header", () => {
    useChatStore.setState({
      sessionCostUsd: 0.42,
      sessionUsageByModel: {
        "model-a": {
          inputTokens: 1000,
          outputTokens: 500,
          totalTokens: 1500,
          cacheReadInputTokens: null,
          cacheCreationInputTokens: null,
          totalCostUsd: 0.42,
        },
      },
    });
    const onClose = vi.fn();
    render(<SessionOverview onClose={onClose} />);

    expect(screen.getByTestId("session-overview-cost")).toHaveTextContent("$0.42");
    expect(screen.getByTestId("agent-info-model-model-a")).toHaveTextContent("1.5K");
    fireEvent.click(screen.getByRole("button", { name: "Hide session overview" }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
