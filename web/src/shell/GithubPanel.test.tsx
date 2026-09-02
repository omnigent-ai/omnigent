// Tests for GithubPanel — the stacked "Files changed" view. The GitHub data
// hooks and the heavy MonacoDiffViewer are mocked; IntersectionObserver (absent
// in jsdom) is stubbed to fire immediately so lazy sections mount.

import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GithubChangedFile, GithubInfo } from "@/hooks/useGithub";

const state = vi.hoisted(() => ({
  info: null as {
    data?: GithubInfo;
    isLoading: boolean;
    error: unknown;
    isFetching: boolean;
  } | null,
  changes: null as {
    data?: { available: boolean; data: GithubChangedFile[] };
    isLoading: boolean;
    error: unknown;
    isFetching: boolean;
  } | null,
}));

vi.mock("@/hooks/useGithub", () => ({
  useGithubInfo: () => state.info,
  useGithubChangedFiles: () => state.changes,
  // Diff is only requested once a section is "seen" (path non-null).
  useGithubFileDiff: (_c: string, path: string | null) =>
    path
      ? {
          data: { object: "session.github.file_diff", path, before: "old", after: "new" },
          isLoading: false,
          error: null,
        }
      : { data: undefined, isLoading: false, error: null },
}));

// The diff engine is exercised in MonacoDiffViewer.test; here we only need to
// see that a section renders its file's diff.
vi.mock("./MonacoDiffViewer", () => ({
  MonacoDiffViewer: ({ path }: { path: string }) => <div data-testid="diff" data-path={path} />,
}));

import { GithubPanel } from "./GithubPanel";

function file(
  path: string,
  status: GithubChangedFile["status"],
  adds = 1,
  dels = 0,
): GithubChangedFile {
  return {
    path,
    name: path.split("/").pop() ?? path,
    status,
    bytes: null,
    modified_at: null,
    lines_added: adds,
    lines_removed: dels,
  };
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <GithubPanel conversationId="conv_1" />
    </QueryClientProvider>,
  );
}

let scrollIntoView: ReturnType<typeof vi.fn>;

beforeEach(() => {
  // Fire the observer callback immediately on observe so lazy sections mount.
  class IO {
    private cb: IntersectionObserverCallback;
    constructor(cb: IntersectionObserverCallback) {
      this.cb = cb;
    }
    observe(el: Element) {
      this.cb(
        [{ isIntersecting: true, target: el } as IntersectionObserverEntry],
        this as unknown as IntersectionObserver,
      );
    }
    unobserve() {}
    disconnect() {}
    takeRecords(): IntersectionObserverEntry[] {
      return [];
    }
  }
  vi.stubGlobal("IntersectionObserver", IO);
  scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView as unknown as Element["scrollIntoView"];

  state.info = {
    data: {
      object: "session.github.info",
      available: true,
      gh_available: true,
      authenticated: true,
      branch: "test/pr-view",
      base_ref: "main",
      repo: { name_with_owner: "acme/app" },
      pr: {
        number: 6000,
        title: "chore: dummy PR",
        state: "OPEN",
        url: "https://example.com/pr/6000",
        is_draft: false,
        author: "dev",
        base_ref: "main",
        head_ref: "test/pr-view",
        checks: { passing: 66, failing: 2, pending: 0, total: 68 },
      },
    },
    isLoading: false,
    error: null,
    isFetching: false,
  };
  state.changes = {
    data: {
      available: true,
      data: [file("hello.py", "created"), file("src/app.ts", "modified", 3, 1)],
    },
    isLoading: false,
    error: null,
    isFetching: false,
  };
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("GithubPanel", () => {
  it("shows the PR header with title and CI check counts", async () => {
    renderPanel();
    expect(await screen.findByText("chore: dummy PR")).toBeInTheDocument();
    expect(screen.getByText("#6000")).toBeInTheDocument();
    // The check counts are labeled as checks (tooltip), not a diffstat.
    const checks = screen.getByTitle(/Checks: 66 passing, 2 failing/);
    expect(checks).toHaveTextContent("66");
    expect(checks).toHaveTextContent("2");
  });

  it("stacks a diff section per changed file", async () => {
    renderPanel();
    const diffs = await screen.findAllByTestId("diff");
    expect(diffs.map((d) => d.getAttribute("data-path"))).toEqual(["hello.py", "src/app.ts"]);
  });

  it("jumps to a file's section when its sidebar row is clicked", async () => {
    renderPanel();
    await screen.findAllByTestId("diff");
    // The sidebar row is a button; the section header with the same name is not.
    const row = screen.getAllByRole("button", { name: /app\.ts/ })[0];
    fireEvent.click(row);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start" });
  });

  it("renders a non-git workspace message without a PR body", () => {
    state.info = {
      data: { object: "session.github.info", available: false, reason: "not_a_git_repo" },
      isLoading: false,
      error: null,
      isFetching: false,
    };
    renderPanel();
    expect(screen.getByText(/isn.t a git repository/)).toBeInTheDocument();
  });
});
