import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import {
  type PullRequestPanelView,
  pullRequestPageViewKey,
  pullRequestPanelViewKey,
  showPullRequestInPanel,
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

  it("steers Classic and the new page alike, under keys of their own", () => {
    const client = new QueryClient();
    const pageView = () => client.getQueryData(pullRequestPageViewKey("conv_1"));
    showPullRequestInPanel(client, "conv_1", PR);
    expect(pageView()).toEqual(view(client, "conv_1"));
    expect(pullRequestPageViewKey("conv_1")).not.toEqual(pullRequestPanelViewKey("conv_1"));
  });
});
