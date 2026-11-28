import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BranchDiscoveryNotice } from "@/hooks/useBranchPullRequest";
import {
  type BranchDiscovery,
  resetBranchChoicesForTests,
  withBranchChoice,
} from "@/lib/branchPullRequest";
import type * as IdentityModule from "@/lib/identity";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { BranchPullRequestNotice } from "./BranchPullRequestNotice";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));

const candidate = (number: number) => ({
  url: `https://github.com/acme/app/pull/${number}`,
  host: "github.com",
  number,
  title: `Feature part ${number}`,
  state: "OPEN" as const,
  is_draft: false,
  base_repository: "acme/app",
});

const KEY = { sessionId: "conv_1", branch: "feature" };
const discovery = (result: BranchDiscovery["result"]): BranchDiscovery => ({
  choiceKey: KEY,
  result,
});
const ambiguous = (incomplete = false) => ({
  status: "ambiguous" as const,
  discovery: discovery({
    status: "ambiguous",
    candidates: [candidate(1), candidate(2)],
    incomplete,
  }),
});

function renderNotice(notice: BranchDiscoveryNotice | undefined) {
  const onChanged = vi.fn();
  const view = render(
    <QueryClientProvider client={new QueryClient()}>
      <BranchPullRequestNotice notice={notice} onChanged={onChanged} />
    </QueryClientProvider>,
  );
  return { onChanged, view };
}

const paletteCommands = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);

afterEach(() => resetBranchChoicesForTests());

describe("BranchPullRequestNotice", () => {
  it.each([
    ["#2 Feature part 2", { status: "found", evidence: "choice", pull_request: candidate(2) }],
    ["None of these", { status: "none", chosen: true }],
  ] as const)("asks which PR is the branch's, and keeps %s for this run", async (label, after) => {
    renderNotice(ambiguous());
    expect(
      screen.getByRole("heading", { name: "Which pull request is this branch?" }),
    ).toBeVisible();
    expect(
      screen.getByText("This app won’t remember your choice after it restarts."),
    ).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: label }));
    expect(withBranchChoice(ambiguous().discovery.result, KEY)).toEqual(after);
  });

  it("offers the same choices in ⌘K, and they work", () => {
    renderNotice(ambiguous(true));
    expect(paletteCommands().map((command) => command.label)).toEqual([
      "Choose #1 as this branch’s pull request",
      "Choose #2 as this branch’s pull request",
      "This branch has none of these pull requests",
    ]);
    expect(screen.getByText(/older ones aren’t listed/)).toBeVisible();
    paletteCommands()[0]!.run();
    expect(withBranchChoice(ambiguous().discovery.result, KEY)).toMatchObject({
      status: "found",
      pull_request: { number: 1 },
    });
  });

  it("says there's none once chosen, and offers choosing again", async () => {
    renderNotice({ status: "none-chosen", discovery: discovery({ status: "none", chosen: true }) });
    expect(screen.getByText("No pull request for this branch.")).toBeVisible();
    expect(paletteCommands().map((command) => command.label)).toEqual([
      "Choose this branch’s pull request again",
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Choose again" }));
    expect(withBranchChoice(ambiguous().discovery.result, KEY).status).toBe("ambiguous");
  });

  it("shows a failure with retry", async () => {
    const { onChanged } = renderNotice({
      status: "failed",
      error: new Error("GitHub couldn't list this branch's pull requests. Try again."),
    });
    expect(
      screen.getByText("GitHub couldn't list this branch's pull requests. Try again."),
    ).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onChanged).toHaveBeenCalled();
  });

  it.each([
    ["while still searching", { status: "searching" as const }],
    ["without a lookup", undefined],
  ])("shows nothing %s", (_name, notice) => {
    const { view } = renderNotice(notice);
    expect(view.container).toBeEmptyDOMElement();
  });
});
