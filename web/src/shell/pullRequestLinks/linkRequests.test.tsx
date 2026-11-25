import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { openPullRequestTabFor, useRegisterPullRequestTabOpener } from "./linkRequests";

const PULL = { kind: "pull", url: "https://github.com/o/r/pull/1" } as const;
const OPEN = { classic: "open" } as const;

function Link({
  sessionId,
  openTab,
}: {
  sessionId: string;
  openTab: Parameters<typeof useRegisterPullRequestTabOpener>[1];
}) {
  useRegisterPullRequestTabOpener(sessionId, openTab);
  return null;
}

afterEach(cleanup);

describe("which chat opens the Pull Requests tab", () => {
  it("keeps a chat's opener while any of its links is still on screen", () => {
    // Every link in one chat registers that chat's opener.
    const openTab = vi.fn();
    const links = (count: number) =>
      Array.from({ length: count }, (_, index) => (
        <Link key={index} sessionId="conv_1" openTab={openTab} />
      ));
    const view = render(<>{links(2)}</>);
    // One link scrolls away or its message is removed; the other stays.
    view.rerender(<>{links(1)}</>);
    expect(openPullRequestTabFor("conv_1", PULL, OPEN)).toBe(true);
    expect(openTab).toHaveBeenCalledTimes(1);
    // The chat is told which pull request to show, and how.
    expect(openTab).toHaveBeenCalledWith(PULL, OPEN);
    // The chat leaves the screen with its last link.
    view.rerender(<>{links(0)}</>);
    expect(openPullRequestTabFor("conv_1", PULL, OPEN)).toBe(false);
    expect(openTab).toHaveBeenCalledTimes(1);
  });

  it("opens only a chat still showing the session", () => {
    const first = vi.fn();
    const second = vi.fn();
    const view = render(
      <>
        <Link sessionId="conv_1" openTab={first} />
        <Link sessionId="conv_2" openTab={second} />
      </>,
    );
    expect(openPullRequestTabFor("conv_2", PULL, OPEN)).toBe(true);
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
    // The first pane switches to another session.
    view.rerender(
      <>
        <Link sessionId="conv_3" openTab={first} />
        <Link sessionId="conv_2" openTab={second} />
      </>,
    );
    expect(openPullRequestTabFor("conv_1", PULL, OPEN)).toBe(false);
    expect(first).not.toHaveBeenCalled();
  });
});
