import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { registeredPaletteCommands } from "@/test/paletteCommands";

import {
  resetWorkspaceTabsSessionsForTests,
  workspaceTabsSession,
} from "../workspaceTabs/workspaceTabsSessions";
import { NO_PULL_REQUESTS_REASON, SHOW_ALL_PULL_REQUESTS_LABEL } from "./model";
import { ShowAllPullRequestsCommand } from "./useShowAllPullRequestsCommand";

const rows = () =>
  registeredPaletteCommands().filter((command) => command.label === SHOW_ALL_PULL_REQUESTS_LABEL);

afterEach(() => {
  cleanup();
  resetWorkspaceTabsSessionsForTests();
});

describe("ShowAllPullRequestsCommand", () => {
  it("offers one ⌘K row for the session that shows its pull requests", () => {
    const showAll = vi.fn();
    render(
      <ShowAllPullRequestsCommand sessionId="conv_1" pullRequestsAvailable showAll={showAll} />,
    );
    expect(rows()).toHaveLength(1);
    const [row] = rows();
    expect(row).toMatchObject({ id: "workspace:conv_1:pull-requests:all", movesFocus: true });
    expect(row!.availability?.available).toBe(true);
    row!.run();
    expect(showAll).toHaveBeenCalledOnce();
  });

  it("is unavailable where the workspace can't show pull requests, until a pull request tab opens", async () => {
    render(
      <ShowAllPullRequestsCommand
        sessionId="conv_2"
        pullRequestsAvailable={false}
        showAll={vi.fn()}
      />,
    );
    expect(rows()[0]!.availability).toEqual({ available: false, reason: NO_PULL_REQUESTS_REASON });

    await act(async () => {
      await workspaceTabsSession("conv_2").store.open(
        { place: { kind: "pulls" } },
        { opener: "user" },
      );
    });
    expect(rows()[0]!.availability?.available).toBe(true);
  });

  it("withdraws its row when it unmounts", () => {
    const { unmount } = render(
      <ShowAllPullRequestsCommand sessionId="conv_3" pullRequestsAvailable showAll={vi.fn()} />,
    );
    unmount();
    expect(rows()).toHaveLength(0);
  });
});
