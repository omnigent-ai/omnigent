// "Changes are made as @login" in a PR's pickers is left out only while the PR page's header
// shows that same login by name. The header's hidden measuring copies always render a full
// face; they must never count, so a header narrowed to its icon keeps the line.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Tabs } from "@/components/ui/tabs";
import type * as PullRequestsModule from "@/newPullRequestPage/hooks/usePullRequests";
import { gitProviderCopy } from "@/lib/gitProviders";
import { MacWriteActingAccount, VisibleMacAccountsProvider } from "@/shell/MacWriteNotices";

import type { PullRequestDetailsState } from "./detailsLayout";
import type * as HeaderFitModule from "./pullRequestHeaderFit";
import type { PullRequestHeaderFit } from "./pullRequestHeaderFit";
import { PullRequestPageHeader } from "./PullRequestPageHeader";
import type { ReadSource } from "./useReadSource";

vi.mock("./PullRequestLifecycleControls", () => ({
  PullRequestStateMenu: ({ badge }: { badge: React.ReactNode }) => badge,
  usePullRequestStatePillProgress: () => ({ kind: "idle" }),
  PullRequestLifecycleOwner: () => null,
  PullRequestChangeNotice: () => null,
}));

const PR_URL = "https://github.com/acme/app/pull/362";
const actor = vi.hoisted(() => ({ login: "octo", host: "github.com" }));
vi.mock("@/newPullRequestPage/hooks/usePullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof PullRequestsModule>()),
  useMacWriteRoute: () => ({
    target: {
      provider: "github",
      host: actor.host,
      repository: "acme/app",
      number: 362,
      url: PR_URL,
    },
    actor: { login: actor.login, generation: 1 },
    reader: null,
  }),
}));

const fit = vi.hoisted(() => ({ current: null as unknown as PullRequestHeaderFit }));
vi.mock("./pullRequestHeaderFit", async (importOriginal) => ({
  ...(await importOriginal<typeof HeaderFitModule>()),
  usePullRequestHeaderFit: () => fit.current,
}));
const FULL: PullRequestHeaderFit = {
  name: "full",
  spacing: "roomy",
  lifecycle: "full",
  stack: "full",
  status: "full",
  source: "full",
  sections: "segments",
};

const readSource = vi.hoisted(() => ({ current: null as unknown as ReadSource }));
vi.mock("./useReadSource", () => ({ useReadSource: () => readSource.current }));
// The repository's account group has its own tests; these offer none.
vi.mock("./useRepoAccount", () => ({ useRepoAccount: () => null }));

function source(over: Partial<ReadSource> = {}): ReadSource {
  return {
    offered: true,
    choice: "automatic",
    route: { kind: "mac", target: {} as never },
    automaticPick: "mac",
    provider: "github",
    repo: { kind: "none" },
    host: "github.com",
    login: "octo",
    choose: vi.fn(),
    ...over,
  };
}

const state = {
  conversationId: "conv_1",
  copy: gitProviderCopy("github"),
  repository: "acme/app",
  pr: { number: 362, url: PR_URL, state: "OPEN", isDraft: false, title: "Collect diagnostics" },
  selectedPrUrl: null,
  titleEditable: false,
  files: [],
  readPath: null,
  lifecycle: {
    conversationId: "conv_1",
    prUrl: PR_URL,
    number: 362,
    state: "OPEN",
    isDraft: false,
    headSha: "a".repeat(40),
    autoMergeOn: false,
    mergeable: false,
  },
  active: true,
  tabs: {
    shown: "summary",
    select: () => {},
    comments: { count: 0, partial: false },
    commits: { shown: false },
  },
} as unknown as PullRequestDetailsState;

// The header's Review menu reads the session's route, which a page always has a client for.
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
);

/** The PR page's header beside a picker's acting-account line, as the details render them. */
function renderPage() {
  return render(
    <MemoryRouter>
      <VisibleMacAccountsProvider>
        <Tabs value="summary">
          <PullRequestPageHeader state={state} options={{}} />
        </Tabs>
        <MacWriteActingAccount prUrl={PR_URL} />
      </VisibleMacAccountsProvider>
    </MemoryRouter>,
    { wrapper },
  );
}

const actingLine = () => screen.queryByText(/^Changes are made as/);

beforeEach(() => {
  fit.current = FULL;
  readSource.current = source();
  actor.login = "octo";
  actor.host = "github.com";
});

describe("the acting account beside the PR page's header", () => {
  it("is left out while the header names the same account", () => {
    renderPage();
    // The header's measuring copies are mounted too.
    expect(document.querySelector("[data-header-sizers]")).not.toBeNull();
    expect(actingLine()).toBeNull();
  });

  it("is shown while the header has room for its icon alone, despite its measuring copies", () => {
    fit.current = { ...FULL, source: "icon" };
    renderPage();
    expect(document.querySelector('[data-header-source="full"]')).not.toBeNull();
    expect(actingLine()).toHaveTextContent("Changes are made as @octo");
  });

  it("comes back when the header narrows to its icon, and goes again when it widens", () => {
    const view = renderPage();
    expect(actingLine()).toBeNull();
    fit.current = { ...FULL, source: "icon" };
    view.rerender(
      <MemoryRouter>
        <VisibleMacAccountsProvider>
          <Tabs value="summary">
            <PullRequestPageHeader state={state} options={{}} />
          </Tabs>
          <MacWriteActingAccount prUrl={PR_URL} />
        </VisibleMacAccountsProvider>
      </MemoryRouter>,
    );
    expect(actingLine()).toHaveTextContent("@octo");
  });

  it("is shown where changes go as another account, or the header names none", () => {
    actor.login = "someone-else";
    const other = renderPage();
    expect(actingLine()).toHaveTextContent("@someone-else");
    other.unmount();

    actor.login = "octo";
    readSource.current = source({ login: null });
    const unknown = renderPage();
    expect(actingLine()).toHaveTextContent("@octo");
    unknown.unmount();

    readSource.current = source({ route: { kind: "host" } });
    renderPage();
    expect(actingLine()).toHaveTextContent("@octo");
  });

  it("is shown for the same login on another host", () => {
    actor.host = "ghe.example.com";
    renderPage();
    expect(actingLine()).toHaveTextContent("@octo");
  });

  it("is shown again once the header is gone", () => {
    const view = render(
      <MemoryRouter>
        <VisibleMacAccountsProvider>
          <Tabs value="summary">
            <PullRequestPageHeader state={state} options={{}} />
          </Tabs>
          <MacWriteActingAccount prUrl={PR_URL} />
        </VisibleMacAccountsProvider>
      </MemoryRouter>,
      { wrapper },
    );
    expect(actingLine()).toBeNull();
    view.rerender(
      <MemoryRouter>
        <VisibleMacAccountsProvider>
          <MacWriteActingAccount prUrl={PR_URL} />
        </VisibleMacAccountsProvider>
      </MemoryRouter>,
    );
    expect(actingLine()).toHaveTextContent("@octo");
  });

  it("is shown outside any page that names accounts, as in a session's PR panel", () => {
    render(<MacWriteActingAccount prUrl={PR_URL} />);
    expect(actingLine()).toHaveTextContent("@octo");
  });
});
