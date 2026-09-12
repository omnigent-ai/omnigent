import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ComposerPrLink } from "./ComposerPrLink";

afterEach(cleanup);

describe("ComposerPrLink", () => {
  it("renders nothing when there are no PRs", () => {
    const { container } = render(<ComposerPrLink prCount={0} prNumber={null} onOpen={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when there is no way to open the tab", () => {
    const { container } = render(<ComposerPrLink prCount={1} prNumber={42} onOpen={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a single PR number and opens the tab on click", () => {
    const onOpen = vi.fn();
    render(<ComposerPrLink prCount={1} prNumber={42} onOpen={onOpen} />);
    const link = screen.getByTestId("composer-pr-link");
    expect(link).toHaveTextContent("#42");
    expect(link).toHaveAttribute("title", "View this PR in the GitHub tab");
    fireEvent.click(link);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("summarizes multiple PRs as a count", () => {
    render(<ComposerPrLink prCount={3} prNumber={42} onOpen={() => {}} />);
    const link = screen.getByTestId("composer-pr-link");
    expect(link).toHaveTextContent("3 PRs");
    expect(link).toHaveAttribute("title", "View these PRs in the GitHub tab");
  });
});
