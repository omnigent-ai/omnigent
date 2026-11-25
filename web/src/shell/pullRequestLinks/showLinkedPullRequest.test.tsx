import { QueryClient } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { pullRequestPanelViewKey } from "../pullRequestPanelView";
import { useRegisterPullRequestTabOpener } from "./linkRequests";
import { showLinkedPullRequest } from "./showLinkedPullRequest";

const URL = "https://github.com/o/r/pull/12";

function Chat({ show }: { show: Parameters<typeof useRegisterPullRequestTabOpener>[1] }) {
  useRegisterPullRequestTabOpener("conv_1", show);
  return null;
}

afterEach(cleanup);

describe("showLinkedPullRequest", () => {
  it.each(["open", "stay"] as const)(
    "asks the chat to show the linked pull request itself (Classic: %s)",
    (classic) => {
      const show = vi.fn();
      render(<Chat show={show} />);
      const client = new QueryClient();
      showLinkedPullRequest(client, "conv_1", URL, classic);
      expect(show).toHaveBeenCalledWith({ kind: "pull", url: URL }, { classic });
      // Classic's panel opens on it whenever it next shows.
      expect(client.getQueryData(pullRequestPanelViewKey("conv_1"))).toEqual({
        selected: URL,
        showingDetails: true,
        subtab: "summary",
      });
    },
  );

  it("does nothing more when no chat shows the session", () => {
    const client = new QueryClient();
    expect(() => showLinkedPullRequest(client, "conv_gone", URL, "open")).not.toThrow();
  });
});
