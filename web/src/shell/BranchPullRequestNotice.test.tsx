import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BranchDiscoveryNotice } from "@/hooks/useBranchPullRequest";
import type { BranchDiscovery } from "@/lib/branchPullRequest";
import { resetBranchChoicesForTests } from "@/lib/branchPullRequest";
import { DesktopPullRequestError } from "@/lib/desktopPullRequests";
import { findsBranchOnSessionMachine, resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import type * as IdentityModule from "@/lib/identity";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { BranchPullRequestNotice } from "./BranchPullRequestNotice";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));

const candidate = (number: number) => ({
  url: `https://github.com/acme/app/pull/${number}`,
  number,
  title: `Feature part ${number}`,
  state: "OPEN" as const,
  is_draft: false,
  base_repository: "acme/app",
  head_repository: "me/app",
  head_ref: "feature",
  head_sha: null,
});

const discovery = (
  result: BranchDiscovery["result"],
  { remembered = true, commit = null as string | null } = {},
): BranchDiscovery => ({
  contextKey: "key-a",
  provider: "github",
  host: "github.com",
  result,
  choiceKey: {
    sessionId: "conv_1",
    branch: commit ? null : "feature",
    commit,
    repositoryContext: "1".repeat(64),
    provider: "github",
    host: "github.com",
  },
  remembered,
});

function installShell({ remembers = true } = {}) {
  const localPullRequestLinks = vi.fn(async () => ({ ok: true, data: [] }));
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    localPullRequestLinks,
    getDesktopFeatures: async () => ({
      localPullRequestLinks: true,
      ...(remembers ? { localPullRequestBranchChoices: 1 } : {}),
    }),
  };
  return localPullRequestLinks;
}

function renderNotice(notice: BranchDiscoveryNotice | undefined, hostCanAnswer = true) {
  const onChanged = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <BranchPullRequestNotice
        sessionId="conv_1"
        notice={notice}
        hostCanAnswer={hostCanAnswer}
        onChanged={onChanged}
      />
    </QueryClientProvider>,
  );
  return onChanged;
}

const paletteLabels = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) =>
    group.commands.map((command) => command.label),
  );

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  resetDesktopReadPathForTests();
  resetBranchChoicesForTests();
});

describe("BranchPullRequestNotice", () => {
  it("asks which PR is the branch's, and saves the choice", async () => {
    const links = installShell();
    const onChanged = renderNotice({
      status: "ambiguous",
      discovery: discovery({
        status: "ambiguous",
        candidates: [candidate(1), candidate(2)],
        incomplete: false,
      }),
    });
    expect(
      screen.getByRole("heading", { name: "Which pull request is this branch?" }),
    ).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "#2 Feature part 2" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(links).toHaveBeenCalledWith(
      expect.objectContaining({
        action: "choose_branch",
        branch: "feature",
        choice: { url: "https://github.com/acme/app/pull/2" },
      }),
    );
  });

  it("says on a shell that can't keep choices that this one won't last a restart", async () => {
    const links = installShell({ remembers: false });
    const onChanged = renderNotice({
      status: "ambiguous",
      discovery: discovery(
        { status: "ambiguous", candidates: [candidate(1), candidate(2)], incomplete: false },
        { remembered: false },
      ),
    });
    expect(
      screen.getByText("This app won’t remember your choice after it restarts."),
    ).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "#1 Feature part 1" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(links).not.toHaveBeenCalled();
  });

  it("doesn't mention restarts when the choice is remembered", () => {
    installShell();
    renderNotice({
      status: "ambiguous",
      discovery: discovery({ status: "ambiguous", candidates: [candidate(1)], incomplete: false }),
    });
    expect(screen.queryByText(/won’t remember your choice/)).toBeNull();
  });

  describe("on a detached HEAD", () => {
    const detached = () => ({
      status: "ambiguous" as const,
      discovery: discovery(
        { status: "ambiguous", candidates: [candidate(1), candidate(2)], incomplete: false },
        { remembered: false, commit: "c".repeat(40) },
      ),
    });

    it.each(["#1 Feature part 1", "#2 Feature part 2", "None of these"])(
      "asks about the commit and accepts %s for this run",
      async (label) => {
        const links = installShell();
        const onChanged = renderNotice(detached());
        expect(
          screen.getByRole("heading", { name: "Which pull request is this commit?" }),
        ).toBeVisible();
        expect(
          screen.getByText("This app won’t remember your choice after it restarts."),
        ).toBeVisible();
        await userEvent.click(screen.getByRole("button", { name: label }));
        await waitFor(() => expect(onChanged).toHaveBeenCalled());
        expect(links).not.toHaveBeenCalled();
      },
    );

    it("offers the same choices in ⌘K, and they work", async () => {
      installShell();
      const onChanged = renderNotice(detached());
      const commands = Object.values(usePaletteCommandsStore.getState().groups).flatMap(
        (group) => group.commands,
      );
      expect(commands.map((command) => command.label)).toEqual([
        "Choose #1 as this commit’s pull request",
        "Choose #2 as this commit’s pull request",
        "This commit has none of these pull requests",
      ]);
      commands[1]!.run();
      await waitFor(() => expect(onChanged).toHaveBeenCalled());
    });
  });

  it("offers the same choices in ⌘K", () => {
    installShell();
    renderNotice({
      status: "ambiguous",
      discovery: discovery({ status: "ambiguous", candidates: [candidate(1)], incomplete: true }),
    });
    expect(paletteLabels()).toEqual([
      "Choose #1 as this branch’s pull request",
      "This branch has none of these pull requests",
    ]);
    expect(screen.getByText(/may not be every pull request/)).toBeVisible();
  });

  it("says there's none once chosen, and offers choosing again", async () => {
    const links = installShell();
    renderNotice({ status: "none-chosen", discovery: discovery({ status: "none", chosen: true }) });
    expect(screen.getByText("No pull request for this branch.")).toBeVisible();
    expect(paletteLabels()).toEqual(["Choose this branch’s pull request again"]);
    await userEvent.click(screen.getByRole("button", { name: "Choose again" }));
    await waitFor(() =>
      expect(links).toHaveBeenCalledWith(expect.objectContaining({ choice: null })),
    );
  });

  it("offers the session's machine when this Mac couldn't check every repository", async () => {
    installShell();
    const onChanged = renderNotice({
      status: "incomplete",
      discovery: discovery({ status: "incomplete" }),
    });
    await userEvent.click(
      screen.getByRole("button", { name: "Use the session’s machine instead" }),
    );
    expect(findsBranchOnSessionMachine("conv_1")).toBe(true);
    expect(onChanged).toHaveBeenCalled();
  });

  it("shows a failure with retry, and no session machine while it's unreachable", async () => {
    installShell();
    const onChanged = renderNotice(
      {
        status: "failed",
        error: new DesktopPullRequestError(
          { code: "rate_limited", message: "GitHub is limiting requests from this Mac." },
          { provider: "github", host: "github.com" },
        ),
        provider: "github",
        host: "github.com",
      },
      false,
    );
    expect(screen.getByText("GitHub is limiting requests from this Mac.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Use the session’s machine instead" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onChanged).toHaveBeenCalled();
  });

  it("shows nothing while still searching", () => {
    installShell();
    const { container } = render(
      <BranchPullRequestNotice
        sessionId="conv_1"
        notice={{ status: "searching" }}
        hostCanAnswer
        onChanged={() => {}}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
