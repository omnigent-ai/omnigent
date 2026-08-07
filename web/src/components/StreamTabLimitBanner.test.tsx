import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockUseStreamTabCount, mockLowLimit } = vi.hoisted(() => ({
  mockUseStreamTabCount: vi.fn(),
  mockLowLimit: vi.fn(),
}));

vi.mock("@/hooks/useStreamTabCount", () => ({
  useStreamTabCount: mockUseStreamTabCount,
}));
vi.mock("@/lib/streamTabRegistry", () => ({
  connectionHasLowStreamLimit: mockLowLimit,
}));

import { StreamTabLimitBanner } from "./StreamTabLimitBanner";

/** Default to the case where the connection cap actually binds (HTTP/1.1). */
function setup(tabCount: number, lowLimit = true): void {
  mockUseStreamTabCount.mockReturnValue(tabCount);
  mockLowLimit.mockReturnValue(lowLimit);
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("StreamTabLimitBanner", () => {
  it("stays hidden while tab count is below the warning threshold", () => {
    setup(4);
    const { container } = render(<StreamTabLimitBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("warns once enough tabs hold a stream to threaten the connection pool", () => {
    setup(5);
    render(<StreamTabLimitBanner />);
    expect(screen.getByRole("status")).toHaveTextContent("5 tabs have a conversation open");
  });

  it("stays hidden on multiplexed connections where the cap does not bind", () => {
    // HTTP/2 / HTTP/3: N streams share one connection, so there is nothing to
    // warn about and warning anyway would be a false alarm in production.
    setup(8, false);
    const { container } = render(<StreamTabLimitBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("hides after dismissal", () => {
    setup(5);
    render(<StreamTabLimitBanner />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("re-warns when the situation worsens after a dismissal", () => {
    setup(5);
    const { rerender } = render(<StreamTabLimitBanner />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    // Opening yet another conversation tab is new information — the pool is
    // now fuller than when the user dismissed.
    setup(6);
    rerender(<StreamTabLimitBanner />);
    expect(screen.getByRole("status")).toHaveTextContent("6 tabs have a conversation open");
  });

  it("stays dismissed when the count drops back", () => {
    setup(6);
    const { rerender } = render(<StreamTabLimitBanner />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    setup(5);
    rerender(<StreamTabLimitBanner />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
