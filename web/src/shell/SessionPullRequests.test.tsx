import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GithubSessionPullList } from "@/lib/githubIntegration";
import { fetchSessionPulls } from "@/lib/githubIntegration";

import { SessionPullRequests } from "./SessionPullRequests";

vi.mock("@/lib/githubIntegration", () => ({
  fetchSessionPulls: vi.fn(),
}));

const fetchMock = vi.mocked(fetchSessionPulls);

function renderPanel(conversationId: string | undefined = "conv_1") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SessionPullRequests conversationId={conversationId} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fetchMock.mockReset();
});

describe("SessionPullRequests", () => {
  it("lists the session's pull requests as links", async () => {
    const data: GithubSessionPullList = {
      connected: true,
      pulls: [
        {
          repo: "caffeinelabs/app",
          number: 12,
          title: "feat: add thing",
          html_url: "https://github.com/caffeinelabs/app/pull/12",
          head_ref: "feat-thing",
          draft: false,
          state: "open",
          merged: false,
          author_login: "octocat",
          created_at: "2026-07-29T01:00:00Z",
        },
        {
          repo: "caffeinelabs/app",
          number: 9,
          title: "chore: merged thing",
          html_url: "https://github.com/caffeinelabs/app/pull/9",
          head_ref: "merged-thing",
          draft: false,
          state: "closed",
          merged: true,
          author_login: "octocat",
          created_at: "2026-07-29T00:30:00Z",
        },
      ],
    };
    fetchMock.mockResolvedValue(data);

    renderPanel();

    const link = await screen.findByRole("link", { name: /feat: add thing/ });
    expect(link).toHaveAttribute("href", "https://github.com/caffeinelabs/app/pull/12");
    expect(screen.getByText("caffeinelabs/app#12")).toBeInTheDocument();
    // Merged/closed PRs are shown too, with the right status badge.
    expect(screen.getByText("caffeinelabs/app#9")).toBeInTheDocument();
    expect(screen.getByText("merged")).toBeInTheDocument();
    expect(screen.getByText("open")).toBeInTheDocument();
  });

  it("renders nothing when there are no PRs", async () => {
    fetchMock.mockResolvedValue({ connected: true, pulls: [] });
    const { container } = renderPanel();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when GitHub is not connected", async () => {
    fetchMock.mockResolvedValue({ connected: false, pulls: [] });
    const { container } = renderPanel();
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing and stays safe without a conversation id", () => {
    // The query is disabled when there's no id; the panel must render nothing
    // rather than throw.
    const { container } = renderPanel(undefined);
    expect(container).toBeEmptyDOMElement();
  });
});
