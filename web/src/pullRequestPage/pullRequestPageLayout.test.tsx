import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PULL_REQUEST_REVIEW_TOOLBAR_HEIGHTS } from "@/components/diff-review/reviewToolbarHeight";
import { PULL_REQUEST_REVIEW_TREE_WIDTH } from "@/components/diff-review/reviewTreeWidth";
import { resetRememberedPaneWidthForTesting } from "@/hooks/useRememberedPaneWidth";
import { gitProviderCopy } from "@/lib/gitProviders";
import { writePanelSharePreference } from "@/lib/panelSizePreferences";
import { installResizableWidth } from "@/test/resizableWidth";

import { PullRequestPagePreview } from "./pullRequestPageLayout";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  resetRememberedPaneWidthForTesting(PULL_REQUEST_REVIEW_TREE_WIDTH);
});

function renderChangesPreview() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <PullRequestPagePreview
          preview={{
            url: "https://example.com/acme/app/pull/362",
            repository: "acme/app",
            number: 362,
            title: "Collect diagnostics",
            state: "OPEN",
            isDraft: false,
            author: null,
          }}
          copy={gitProviderCopy("github")}
          options={{}}
          selectInPlace={() => false}
          section="changes"
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the Changes placeholder while a pull request reads", () => {
  it("draws its files column at the width the loaded review's tree will have", () => {
    writePanelSharePreference("pullRequestReviewTreeShare", 0.3);
    resetRememberedPaneWidthForTesting(PULL_REQUEST_REVIEW_TREE_WIDTH);
    installResizableWidth(1000);
    const { container } = renderChangesPreview();
    const column = container.querySelector<HTMLElement>("[data-diff-review-tree-column]");
    expect(column?.style.width).toBe("300px");
  });

  it("leaves the room the review's toolbar took at this width, and its own height elsewhere", () => {
    // A narrow review whose toolbar wrapped to a second row.
    PULL_REQUEST_REVIEW_TOOLBAR_HEIGHTS.remember(320, 63);
    const toolbar = (container: HTMLElement) =>
      container.querySelector<HTMLElement>("[data-diff-review-placeholder-toolbar]");
    installResizableWidth(320);
    const narrow = renderChangesPreview();
    expect(toolbar(narrow.container)?.style.height).toBe("63px");
    narrow.unmount();
    vi.restoreAllMocks();
    installResizableWidth(330);
    const wider = renderChangesPreview();
    expect(toolbar(wider.container)?.style.height).toBe("");
  });
});
