import { act, cleanup, render } from "@testing-library/react";
import { flushSync } from "react-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SidebarPullRequestPanel } from "./SidebarPullRequestPanel";

const renders = vi.hoisted(() => [] as boolean[]);
vi.mock("../newPullRequestPage/shell/PullRequestPanel", () => ({
  PullRequestPanel: ({ active }: { active?: boolean }) => {
    renders.push(active ?? true);
    return null;
  },
}));

afterEach(() => {
  cleanup();
  renders.length = 0;
});

describe("SidebarPullRequestPanel", () => {
  const onViewChange = () => {};

  it("re-renders the pull request after a sidebar toggle's own commit, not in it", () => {
    const view = render(
      <SidebarPullRequestPanel conversationId="conv_1" active onViewChange={onViewChange} />,
    );
    expect(renders).toEqual([true]);

    act(() => {
      flushSync(() =>
        view.rerender(
          <SidebarPullRequestPanel
            conversationId="conv_1"
            active={false}
            onViewChange={onViewChange}
          />,
        ),
      );
      // The toggle paints before a long review re-renders for it.
      expect(renders).toEqual([true]);
    });
    // Then the panel takes it, pausing its polls.
    expect(renders).toEqual([true, false]);
  });

  it("skips a re-render whose props are the same", () => {
    const view = render(
      <SidebarPullRequestPanel conversationId="conv_1" active onViewChange={onViewChange} />,
    );
    view.rerender(
      <SidebarPullRequestPanel conversationId="conv_1" active onViewChange={onViewChange} />,
    );
    expect(renders).toEqual([true]);
  });
});
