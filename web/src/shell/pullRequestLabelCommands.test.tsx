// The label commands a PR offers ⌘K: Add Label rows from the user's recent labels in its
// repository, sent as the Labels section sends a change, and Edit Labels…, which opens the
// section's picker with its filter focused.

import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";
import type * as PageRouteModule from "@/pullRequestPage/pullRequestPageRoute";
import type { PullRequestLabel } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  recentLabelsOwner,
  recordRecentLabels,
  resetRecentLabelsForTests,
} from "@/lib/recentLabels";
import { resetControlRequestsForTests } from "@/pullRequestPage/controlRequests";
import { PullRequestDestinationContext } from "@/pullRequestPage/pullRequestDestination";
import { isCommandAvailable, usePaletteCommandsStore } from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));
// The session's host serves listing and changing labels, unless a case serves none.
const LABEL_METHODS = ["listLabels", "updateLabels"];
const host = vi.hoisted(() => ({ served: new Set<string>() }));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<typeof PageRouteModule>()),
  usePullRequestMethodServed: (_sessionId: string, method: string) => host.served.has(method),
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => "https://server-a.example.com",
}));

import { PullRequestLabels } from "../newPullRequestPage/shell/PullRequestLabels";
import {
  addRecentLabelCommands,
  editLabelsPaletteLabel,
  PullRequestLabelPaletteCommands,
} from "./pullRequestLabelCommands";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR_URL = "https://github.com/acme/app/pull/7";
const APP = { host: "github.com", repository: "acme/app" };
const LABELS_PATH = "/v1/sessions/conv_1/resources/github/labels";
const label = (name: string, description: string | null = null): PullRequestLabel => ({
  name,
  color: "0e8a16",
  description,
});

describe("addRecentLabelCommands", () => {
  const recent = [label("automerge", "Merge when green"), label("bug"), label("Docs")];

  it("offers each recent label the PR won't have, most recent first, by name alone", () => {
    const commands = addRecentLabelCommands({
      recent,
      applied: new Set(["bug"]),
      waiting: false,
    });
    expect(commands.map((command) => command.paletteLabel)).toEqual([
      "Add Label: automerge",
      "Add Label: Docs",
    ]);
    expect(commands[0]!.keywords).toEqual(
      expect.arrayContaining(["label", "add", "automerge", "Merge when green"]),
    );
    expect(commands.every((command) => isCommandAvailable(command.availability))).toBe(true);
  });

  it("matches applied labels regardless of case", () => {
    const commands = addRecentLabelCommands({
      recent,
      applied: new Set(["automerge", "docs"]),
      waiting: false,
    });
    expect(commands.map((command) => command.paletteLabel)).toEqual(["Add Label: bug"]);
    expect(commands[0]!.id).toBe("add-label:bug");
  });

  it("greys every row out, saying why, while the last label change is still going", () => {
    const commands = addRecentLabelCommands({
      recent,
      applied: new Set(),
      waiting: true,
    });
    expect(commands).toHaveLength(3);
    expect(commands.map((command) => command.availability)).toEqual(
      Array(3).fill({ available: false, reason: "Wait for the last label change to finish" }),
    );
  });

  it("offers nothing without recent labels", () => {
    expect(addRecentLabelCommands({ recent: [], applied: new Set(), waiting: false })).toEqual([]);
  });
});

describe("editLabelsPaletteLabel", () => {
  it("names the PR only where ⌘K lists more than one", () => {
    expect(editLabelsPaletteLabel(null)).toBe("Edit Labels…");
    expect(editLabelsPaletteLabel("#7 Build")).toBe("Edit Labels on #7 Build…");
  });
});

interface LabelPost {
  body: { pr_url: string; add: string[]; remove: string[] };
  respond: (response: Response) => void;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

/** Label posts wait for the test to answer; the repository's list answers `repoLabels`. */
function stubFetch(repoLabels: PullRequestLabel[]) {
  const posts: LabelPost[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname !== LABELS_PATH) return Promise.resolve(json({}, 404));
      if (init?.method !== "POST") {
        return Promise.resolve(json({ object: "list", data: repoLabels, has_more: false }));
      }
      return new Promise<Response>((respond) => {
        posts.push({ body: JSON.parse(String(init.body)), respond });
      });
    }),
  );
  return posts;
}

const SHOWN_VIEW = { id: "pr-view", active: true };

/** The open PR's Labels section and its ⌘K rows, in one shown view, as the PR page has them. */
function renderPage({
  labels,
  editable = true,
  active = true,
}: {
  labels: PullRequestLabel[];
  editable?: boolean;
  active?: boolean;
}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = (shown: PullRequestLabel[]) => (
    <QueryClientProvider client={client}>
      <PullRequestDestinationContext.Provider value={SHOWN_VIEW}>
        <PullRequestLabelPaletteCommands
          conversationId="conv_1"
          prUrl={PR_URL}
          labels={shown}
          editable={editable}
          active={active}
        />
        <PullRequestLabels
          conversationId="conv_1"
          prUrl={PR_URL}
          labels={shown}
          editable={editable}
        />
      </PullRequestDestinationContext.Provider>
    </QueryClientProvider>
  );
  const view = render(tree(labels));
  return { rerender: (shown: PullRequestLabel[]) => view.rerender(tree(shown)) };
}

const paletteLabels = () => registeredPaletteCommands().map((command) => command.label);
const runCommand = (name: string) => {
  const command = registeredPaletteCommands().find((candidate) => candidate.label === name);
  if (!command) throw new Error(`No "${name}" among: ${paletteLabels().join(", ")}`);
  act(() => command.run());
};
const chips = () => {
  const list = screen.queryByRole("list", { name: "Labels" });
  return list
    ? within(list)
        .getAllByRole("listitem")
        .map((item) => item.getAttribute("aria-label") ?? item.textContent ?? "")
    : [];
};

describe("PullRequestLabelPaletteCommands", () => {
  beforeEach(() => {
    host.served = new Set(LABEL_METHODS);
    window.localStorage.clear();
    recordRecentLabels(APP, [label("bug")], recentLabelsOwner());
    recordRecentLabels(APP, [label("automerge")], recentLabelsOwner());
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    resetRecentLabelsForTests();
    resetControlRequestsForTests();
    usePaletteCommandsStore.setState({ groups: {} });
  });

  it("adds a recent label from ⌘K alone, as the section sends a change", async () => {
    const posts = stubFetch([]);
    const page = renderPage({ labels: [label("bug")] });
    // The PR has bug already, so only automerge is offered.
    expect(paletteLabels()).toEqual(["Edit Labels…", "Add Label: automerge"]);

    runCommand("Add Label: automerge");
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]!.body).toEqual({ pr_url: PR_URL, add: ["automerge"], remove: [] });
    expect(chips()).toEqual(["automerge: Adding…", "bug"]);
    // Sent, the label is no longer one to add.
    expect(paletteLabels()).toEqual(["Edit Labels…"]);

    await act(async () =>
      posts[0]!.respond(json({ object: "list", data: [label("automerge"), label("bug")] })),
    );
    // The panel's read now has it, as the answer patched it.
    page.rerender([label("automerge"), label("bug")]);
    await waitFor(() => expect(chips()).toEqual(["automerge", "bug"]));
    expect(paletteLabels()).toEqual(["Edit Labels…"]);
  });

  it("opens the Labels picker with its filter focused from Edit Labels…", async () => {
    stubFetch([label("bug"), label("ready")]);
    renderPage({ labels: [] });
    expect(screen.queryByPlaceholderText("Filter labels…")).toBeNull();

    runCommand("Edit Labels…");
    const filter = await screen.findByPlaceholderText("Filter labels…");
    await waitFor(() => expect(filter).toHaveFocus());
  });

  it("offers nothing where the labels can't be edited or the view isn't shown", () => {
    stubFetch([]);
    renderPage({ labels: [], editable: false });
    expect(paletteLabels()).toEqual([]);
    cleanup();
    renderPage({ labels: [], active: false });
    expect(paletteLabels()).toEqual([]);
  });

  it.each(LABEL_METHODS)("offers nothing where the host doesn't serve %s", (method) => {
    stubFetch([]);
    host.served.delete(method);
    renderPage({ labels: [] });
    expect(paletteLabels()).toEqual([]);
  });
});
