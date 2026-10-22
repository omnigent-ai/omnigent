import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { StreamBudgetBanner } from "./StreamBudgetBanner";
import { useChatStore } from "@/store/chatStore";

afterEach(() => {
  cleanup();
  useChatStore.setState({ streamBudgetExceeded: false, streamBudgetBannerDismissed: false });
});

describe("StreamBudgetBanner", () => {
  it("stays hidden while the tab is within budget", () => {
    useChatStore.setState({ streamBudgetExceeded: false, streamBudgetBannerDismissed: false });
    render(<StreamBudgetBanner />);
    expect(screen.queryByText(/a lot of conversations open across your tabs/i)).toBeNull();
  });

  it("warns to close tabs when over budget", () => {
    useChatStore.setState({ streamBudgetExceeded: true, streamBudgetBannerDismissed: false });
    render(<StreamBudgetBanner />);
    expect(screen.getByText(/a lot of conversations open across your tabs/i)).toBeTruthy();
  });

  it("hides once dismissed and records the dismissal", () => {
    useChatStore.setState({ streamBudgetExceeded: true, streamBudgetBannerDismissed: false });
    render(<StreamBudgetBanner />);
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(useChatStore.getState().streamBudgetBannerDismissed).toBe(true);
    expect(screen.queryByText(/a lot of conversations open across your tabs/i)).toBeNull();
  });
});
