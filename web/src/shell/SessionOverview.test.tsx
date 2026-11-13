import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ModelUsage } from "@/lib/types";
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

function modelUsage(totalTokens: number | null, totalCostUsd: number | null): ModelUsage {
  return {
    inputTokens: null,
    outputTokens: null,
    totalTokens,
    cacheReadInputTokens: null,
    cacheCreationInputTokens: null,
    totalCostUsd,
  };
}

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
        <SessionCostButton docked open onOpenChange={() => {}} />
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

  it("lists each model's cost and totals tokens only when every model counts them", () => {
    useChatStore.setState({
      sessionCostUsd: 0.42,
      sessionUsageByModel: { "model-a": modelUsage(500, null), "model-b": modelUsage(1000, 0.42) },
    });
    const onClose = vi.fn();
    render(<SessionOverview onClose={onClose} />);
    const rowTexts = () =>
      [...document.querySelector("[data-slot=card-content]")!.children].map(
        (row) => row.textContent,
      );

    // An unpriced model keeps its row with an empty value.
    expect(rowTexts()).toEqual(["Session cost$0.42", "model-b$0.42", "model-a", "Tokens1.5K"]);

    act(() =>
      useChatStore.setState({
        sessionUsageByModel: {
          "model-a": modelUsage(null, 0.1),
          "model-b": modelUsage(1000, 0.42),
        },
      }),
    );
    expect(rowTexts()).toEqual(["Session cost$0.42", "model-b$0.42", "model-a$0.10"]);

    fireEvent.click(screen.getByRole("button", { name: "Hide session overview" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it.each([true, false])("toggles from the header chip (docked=%s)", (docked) => {
    const onOpenChange = vi.fn();
    const button = (open: boolean) => (
      <SessionCostButton docked={docked} open={open} onOpenChange={onOpenChange}>
        overview card
      </SessionCostButton>
    );
    const { rerender } = render(button(false));
    const chip = () => screen.getByTestId("header-session-cost");
    expect(chip()).not.toHaveAttribute("title");

    fireEvent.click(chip());
    expect(onOpenChange).toHaveBeenCalledExactlyOnceWith(true);

    rerender(button(true));
    expect(chip()).toHaveAttribute("aria-expanded", "true");
    const popover = screen.queryByRole("dialog", { name: "Session overview" });
    if (docked) expect(popover).toBeNull();
    else expect(popover).toHaveTextContent("overview card");
  });
});
