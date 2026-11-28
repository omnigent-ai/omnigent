// The label picker on a big repository: the labels this user added recently come first, typed
// text searches GitHub for labels past the ones listed, and each search outcome says what it is.
// A search failure keeps every listed label and says so, never as an empty result. The label
// hooks are stubbed here; their reads and caching have their own tests.

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as PullRequestsModule from "@/newPullRequestPage/hooks/usePullRequests";
import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";

const PR_URL = "https://github.com/acme/app/pull/7";
const label = (name: string, description: string | null = null) => ({
  name,
  color: "ededed",
  description,
});

interface Query<T> {
  data?: T;
  isPending?: boolean;
  isError?: boolean;
  isSuccess?: boolean;
  isFetching?: boolean;
  isFetched?: boolean;
  error?: Error | null;
}

const hooks = vi.hoisted(() => ({
  repoLabels: {} as Query<{ object: "list"; data: unknown[]; has_more: boolean; restored?: true }>,
  search: {} as Query<{ object: "list"; data: unknown[] }>,
  searchOffered: true,
  searched: [] as { text: string; enabled: boolean }[],
}));

vi.mock("@/newPullRequestPage/hooks/usePullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof PullRequestsModule>()),
  usePullRequestRepoLabels: () => hooks.repoLabels,
  usePullRequestLabelSearch: (
    _conversationId: string,
    _prUrl: string,
    text: string,
    enabled: boolean,
  ) => {
    hooks.searched.push({ text, enabled });
    return hooks.search;
  },
  usePullRequestLabelSearchOffered: () => hooks.searchOffered,
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => "https://server-a.example.com",
}));

import {
  recentLabels,
  recentLabelsOwner,
  recordRecentLabels,
  resetRecentLabelsForTests,
} from "@/lib/recentLabels";

import { PullRequestLabels } from "./PullRequestLabels";

const APP = { host: "github.com", repository: "acme/app" };

function renderPicker() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const picker = <PullRequestLabels conversationId="conv_1" prUrl={PR_URL} labels={[]} editable />;
  render(<QueryClientProvider client={client}>{picker}</QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Edit labels" }));
  return screen.getByRole("dialog");
}

/** Each group the picker lists, by its heading, with its labels in order. */
const groupNames = (dialog: HTMLElement) =>
  [...dialog.querySelectorAll("[cmdk-group]")]
    .filter((group) => !group.hasAttribute("hidden"))
    .map((group) => ({
      heading: group.querySelector("[cmdk-group-heading]")?.textContent ?? "",
      labels: [...group.querySelectorAll("[cmdk-item]")].map((item) =>
        item.getAttribute("data-value"),
      ),
    }));

const type = (dialog: HTMLElement, text: string) =>
  act(() => {
    fireEvent.change(within(dialog).getByPlaceholderText("Filter labels…"), {
      target: { value: text },
    });
  });

beforeEach(() => {
  window.localStorage.clear();
  hooks.repoLabels = {
    data: {
      object: "list",
      data: [label("automerge"), label("bug"), label("docs")],
      has_more: true,
    },
    isPending: false,
    isError: false,
  };
  hooks.search = { isFetching: false, isFetched: false, isSuccess: false, isError: false };
  hooks.searchOffered = true;
  hooks.searched = [];
});

afterEach(() => resetRecentLabelsForTests());

describe("recent labels", () => {
  it("come first, then the repository's others, each once", () => {
    recordRecentLabels(APP, [label("bug")], recentLabelsOwner());
    recordRecentLabels(APP, [label("automerge")], recentLabelsOwner());
    const dialog = renderPicker();
    const [recent, all] = groupNames(dialog);
    expect(recent).toEqual({ heading: "Recent", labels: ["automerge", "bug"] });
    expect(all).toEqual({ heading: "All labels", labels: ["docs"] });
  });

  it("show a label found only by searching GitHub, from what was kept, without a search", () => {
    recordRecentLabels(APP, [label("far-past-the-list", "Merge when green")], recentLabelsOwner());
    const dialog = renderPicker();
    expect(groupNames(dialog)[0]).toEqual({ heading: "Recent", labels: ["far-past-the-list"] });
    expect(within(dialog).getByText("Merge when green")).toBeInTheDocument();
    expect(hooks.searched.every(({ text }) => text === "")).toBe(true);
  });

  it("drop a label a complete list lacks, but not on a list restored from this device", () => {
    recordRecentLabels(APP, [label("gone")], recentLabelsOwner());
    hooks.repoLabels = {
      data: { object: "list", data: [label("bug")], has_more: false, restored: true },
      isPending: false,
    };
    renderPicker();
    expect(recentLabels(APP)).toHaveLength(1);
    cleanup();
    hooks.repoLabels = {
      data: { object: "list", data: [label("bug")], has_more: false },
      isPending: false,
    };
    renderPicker();
    expect(recentLabels(APP)).toEqual([]);
  });
});

describe("a refresh behind the list", () => {
  it("keeps the labels it has, Recent too, selectable when the refresh fails, and says so", () => {
    recordRecentLabels(APP, [label("automerge")], recentLabelsOwner());
    hooks.repoLabels = {
      data: { object: "list", data: [label("automerge"), label("bug")], has_more: false },
      isPending: false,
      isError: true,
      error: new Error("GitHub’s limit was reached."),
    };
    const dialog = renderPicker();
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "Couldn’t refresh labels: GitHub’s limit was reached.",
    );
    expect(within(dialog).getByRole("option", { name: "bug" })).not.toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(groupNames(dialog)[0]).toEqual({ heading: "Recent", labels: ["automerge"] });
  });

  it("shows only the error when there's nothing read to keep", () => {
    hooks.repoLabels = { isPending: false, isError: true, error: new Error("Offline.") };
    const dialog = renderPicker();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Offline.");
    expect(within(dialog).queryAllByRole("option")).toHaveLength(0);
  });
});

describe("searching GitHub past the listed labels", () => {
  it("searches the typed text, and lists GitHub's other matches below the listed ones", () => {
    const dialog = renderPicker();
    type(dialog, "auto");
    expect(hooks.searched.at(-1)).toEqual({ text: "auto", enabled: true });
    hooks.search = {
      data: { object: "list", data: [label("automerge"), label("auto-revert")] },
      isSuccess: true,
      isFetched: true,
      isFetching: false,
    };
    type(dialog, "auto ");
    const groups = groupNames(dialog);
    expect(groups.at(-1)).toEqual({ heading: "More on GitHub", labels: ["auto-revert"] });
  });

  it("says it's searching, then that GitHub has nothing more", () => {
    const dialog = renderPicker();
    hooks.search = { isFetching: true, isFetched: false };
    type(dialog, "zzz");
    expect(within(dialog).getByText("Searching GitHub for more labels…")).toBeInTheDocument();
    hooks.search = {
      data: { object: "list", data: [] },
      isSuccess: true,
      isFetched: true,
      isFetching: false,
    };
    type(dialog, "zzz ");
    expect(within(dialog).queryByText("Searching GitHub for more labels…")).toBeNull();
    expect(within(dialog).getByText("No other labels match on GitHub.")).toBeInTheDocument();
  });

  it("keeps every listed label when the search fails, and says it failed", () => {
    hooks.search = {
      isError: true,
      isFetched: true,
      error: new Error("GitHub’s limit was reached."),
    };
    const dialog = renderPicker();
    type(dialog, "b");
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "Couldn’t search GitHub: GitHub’s limit was reached.",
    );
    expect(within(dialog).getByRole("option", { name: "bug" })).toBeInTheDocument();
    expect(within(dialog).queryByText("No other labels match on GitHub.")).toBeNull();
  });

  it("doesn't search where the list is complete, and drops the cut-off note", () => {
    hooks.repoLabels = {
      data: { object: "list", data: [label("bug")], has_more: false },
      isPending: false,
    };
    const dialog = renderPicker();
    type(dialog, "zzz");
    expect(hooks.searched.at(-1)?.enabled).toBe(false);
    expect(within(dialog).queryByText(/Showing the first/)).toBeNull();
    expect(within(dialog).queryByText(/Searching GitHub/)).toBeNull();
  });

  it("keeps the cut-off note, and doesn't search, on a desktop app that can't", () => {
    hooks.searchOffered = false;
    const dialog = renderPicker();
    type(dialog, "zzz");
    expect(within(dialog).getByText("Showing the first 3 labels.")).toBeInTheDocument();
    expect(within(dialog).queryByText(/Searching GitHub/)).toBeNull();
  });
});

describe("labels checked from a search", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("stays listed, checked, once the search clears, and unchecking it there sends nothing", async () => {
    const fetch = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) => new Response("{}", { status: 404 }),
    );
    vi.stubGlobal("fetch", fetch);
    const remote = label("far-past-the-list", "A label past the first read");
    hooks.search = {
      data: { object: "list", data: [remote] },
      isFetched: true,
      isSuccess: true,
      isFetching: false,
    };
    const dialog = renderPicker();
    type(dialog, "far-past-the-list");
    fireEvent.click(within(dialog).getByRole("option", { name: /^far-past-the-list/ }));
    type(dialog, "");
    const row = within(dialog).getByRole("option", { name: /^far-past-the-list/ });
    expect(row).toHaveAttribute("aria-checked", "true");
    expect(within(dialog).getByText("1 to add · applied when you close")).toBeInTheDocument();

    // Unchecked, it's back to what GitHub has, and nothing lists it unless searched again.
    fireEvent.click(row);
    expect(within(dialog).queryByRole("option", { name: /^far-past-the-list/ })).toBeNull();
    expect(within(dialog).queryByText(/applied when you close/)).toBeNull();
    fireEvent.keyDown(within(dialog).getByPlaceholderText("Filter labels…"), { key: "Escape" });
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toEqual([]);
  });
});
