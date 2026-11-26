import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import {
  DEFAULT_PANEL_VIEW,
  type PullRequestPanelView,
  pullRequestPanelViewKey,
  showPullRequestInPanel,
  showPullRequestListInPanel,
  viewForChosenPullRequest,
} from "./pullRequestPanelView";

const PR = "https://github.com/acme/app/pull/42";
const view = (client: QueryClient, sessionId: string) =>
  client.getQueryData<PullRequestPanelView>(pullRequestPanelViewKey(sessionId));

describe("pull request panel view", () => {
  it("opens a chosen pull request on the reader's section, leaving the last one's commit and file", () => {
    expect(viewForChosenPullRequest(PR, "commits")).toEqual({
      selected: PR,
      showingDetails: true,
      subtab: "commits",
    });
  });

  it("shows a pull request's summary for one session only", () => {
    const client = new QueryClient();
    client.setQueryData(pullRequestPanelViewKey("conv_1"), {
      ...viewForChosenPullRequest("https://github.com/acme/app/pull/1", "changes"),
      commit: "abc123",
    });
    showPullRequestInPanel(client, "conv_1", PR);
    expect(view(client, "conv_1")).toEqual({
      selected: PR,
      showingDetails: true,
      subtab: "summary",
    });
    expect(view(client, "conv_2")).toBeUndefined();
  });

  it("shows the list, keeping the selection and clearing the commit and file", () => {
    const client = new QueryClient();
    showPullRequestListInPanel(client, "conv_1");
    expect(view(client, "conv_1")).toEqual(DEFAULT_PANEL_VIEW);
    client.setQueryData(pullRequestPanelViewKey("conv_1"), {
      ...viewForChosenPullRequest(PR, "changes"),
      commit: "abc123",
      file: { path: "a.ts" },
    });
    showPullRequestListInPanel(client, "conv_1");
    expect(view(client, "conv_1")).toEqual({
      selected: PR,
      showingDetails: false,
      subtab: "summary",
    });
  });
});
