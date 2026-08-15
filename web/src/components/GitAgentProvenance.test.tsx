import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GitAgentProvenance, gitUrlDisplay } from "./GitAgentProvenance";
import { ApiError } from "@/lib/sessionsApi";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";

const refreshMock = vi.fn();
vi.mock("@/lib/agentsApi", () => ({
  refreshAgent: (...args: unknown[]) => refreshMock(...args),
}));

function agent(overrides: Partial<AvailableAgent> = {}): AvailableAgent {
  return {
    id: "ag_git",
    name: "reviewer",
    display_name: "Reviewer",
    description: null,
    harness: null,
    skills: [],
    git_url: "https://github.com/org/repo.git",
    git_ref: "main",
    git_commit: "abcdef1234567890",
    version: 2,
    ...overrides,
  };
}

function renderProvenance(a: AvailableAgent) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <GitAgentProvenance agent={a} />
    </QueryClientProvider>,
  );
}

beforeEach(() => refreshMock.mockReset());
afterEach(cleanup);

describe("gitUrlDisplay", () => {
  it("renders host + path without a trailing .git", () => {
    expect(gitUrlDisplay("https://github.com/org/repo.git")).toBe("github.com/org/repo");
  });
  it("falls back to the raw string for a non-URL", () => {
    expect(gitUrlDisplay("git@github.com:org/repo.git")).toBe("git@github.com:org/repo.git");
  });
});

describe("GitAgentProvenance", () => {
  it("shows the provenance line: host/path @ branch · short-sha · vN", () => {
    renderProvenance(agent());
    const line = screen.getByTestId("git-agent-provenance");
    expect(line).toHaveTextContent("github.com/org/repo");
    expect(line).toHaveTextContent("@ main");
    expect(line).toHaveTextContent("abcdef1"); // 7-char short sha
    expect(line).toHaveTextContent("v2");
  });

  it("refreshes and invalidates the catalog on click", async () => {
    refreshMock.mockResolvedValueOnce({ id: "ag_git", version: 3 });
    renderProvenance(agent());
    fireEvent.click(screen.getByTestId("git-agent-refresh-ag_git"));
    await waitFor(() => expect(refreshMock).toHaveBeenCalledWith("ag_git"));
  });

  it("surfaces a distinct message when the host is offline (409)", async () => {
    refreshMock.mockRejectedValueOnce(new ApiError("host offline", 409, "conflict"));
    renderProvenance(agent());
    fireEvent.click(screen.getByTestId("git-agent-refresh-ag_git"));
    await waitFor(() =>
      expect(screen.getByText(/host offline — connect it to refresh/i)).toBeInTheDocument(),
    );
  });

  it("surfaces the error message for a non-409 failure", async () => {
    refreshMock.mockRejectedValueOnce(new ApiError("branch not found", 400, "invalid_input"));
    renderProvenance(agent());
    fireEvent.click(screen.getByTestId("git-agent-refresh-ag_git"));
    await waitFor(() => expect(screen.getByText(/branch not found/i)).toBeInTheDocument());
  });

  it("does not bubble the Refresh click to the surrounding row (no row-select)", async () => {
    // The component renders inside a DropdownMenuItem whose click selects the
    // agent. Refresh must stopPropagation so clicking it never selects the row.
    refreshMock.mockResolvedValueOnce({ id: "ag_git", version: 3 });
    const rowClick = vi.fn();
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={qc}>
        <div onClick={rowClick} data-testid="row">
          <GitAgentProvenance agent={agent()} />
        </div>
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByTestId("git-agent-refresh-ag_git"));
    await waitFor(() => expect(refreshMock).toHaveBeenCalledWith("ag_git"));
    expect(rowClick).not.toHaveBeenCalled();
  });

  it("marks the button aria-busy and the error as an alert region", async () => {
    refreshMock.mockRejectedValueOnce(new ApiError("boom", 400, "invalid_input"));
    renderProvenance(agent());
    fireEvent.click(screen.getByTestId("git-agent-refresh-ag_git"));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("boom"));
  });
});
