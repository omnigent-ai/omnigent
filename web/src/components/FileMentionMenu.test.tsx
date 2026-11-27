import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FileMentionMenu } from "./FileMentionMenu";

afterEach(cleanup);

const props = {
  currentDir: "",
  activeIndex: -1,
  entries: [],
  loading: true,
  onOpenDir: vi.fn(),
  onAttach: vi.fn(),
};

describe("placement", () => {
  it("opens above the prompt by default and below its first line when placed below", () => {
    const { rerender } = render(<FileMentionMenu {...props} />);
    const wrapper = screen.getByTestId("file-mention-menu").parentElement;
    expect(wrapper?.className).toContain("bottom-full");

    rerender(<FileMentionMenu {...props} placement="below" />);
    const belowWrapper = screen.getByTestId("file-mention-menu").parentElement;
    expect(belowWrapper?.className).toContain("top-11");
    expect(belowWrapper?.className).not.toContain("bottom-full");
    // Capped to the window, the card shrinks to fit and its list is the only scroller.
    expect(belowWrapper?.className).toContain("max-h-[calc(100%-2.75rem)]");
    expect(belowWrapper?.className).not.toContain("overflow-y-auto");
  });
});
