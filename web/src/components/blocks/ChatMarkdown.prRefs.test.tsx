// `#1234` in an assistant reply links to that PR in the session's repo. The
// repo comes from the session's GitHub info, which usually loads after the
// message has rendered, so these tests also pin that a late answer still links.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConversationScopeContext } from "@/components/chat/conversationScope";
import type { PullRequestInfo } from "@/hooks/usePullRequests";
import { writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import type * as NativeBridge from "@/lib/nativeBridge";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

const nativeShell = vi.hoisted(() => ({ native: false }));

vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isNativeShell: () => nativeShell.native,
}));

vi.mock("@/lib/fetchSessionHostId", () => ({
  fetchSessionHostId: vi.fn().mockResolvedValue("local-host"),
}));

const CONVERSATION_ID = "conv_1";
const GITHUB_INFO_ROUTE = `/v1/sessions/${CONVERSATION_ID}/resources/github`;

// GitHub info stays pending unless a test serves it; seeded tests never fetch.
let respondToGithubInfo: () => Response | Promise<Response> = () => new Promise(() => {});
const requestPath = (input: RequestInfo | URL) =>
  new URL(input instanceof Request ? input.url : String(input), window.location.href).pathname;
const fetchMock = vi.fn((input: RequestInfo | URL) =>
  requestPath(input) === GITHUB_INFO_ROUTE
    ? Promise.resolve(respondToGithubInfo())
    : new Promise<Response>(() => {}),
);
const githubInfoRequests = () =>
  fetchMock.mock.calls.filter(([input]) => requestPath(input) === GITHUB_INFO_ROUTE);

function serveGithubInfo(respond: () => Response): void {
  respondToGithubInfo = respond;
}

const FILE_VIEWER = {
  openFile: () => {},
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: CONVERSATION_ID,
  workspaceRoot: "/home/u/ws",
  workspaceHome: "/home/u",
};

let client: QueryClient;

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  respondToGithubInfo = () => new Promise(() => {});
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  client.clear();
  vi.unstubAllGlobals();
  nativeShell.native = false;
  delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
  window.localStorage.clear();
});

function githubInfo(overrides: Partial<PullRequestInfo> = {}): PullRequestInfo {
  return {
    object: "session.github.info",
    available: true,
    repo: { name_with_owner: "acme/app", host: "github.com" },
    ...overrides,
  };
}

function seed(info: PullRequestInfo): void {
  client.setQueryData(["github-info", CONVERSATION_ID], info);
}

function renderMarkdown(
  markdown: string,
  linkPullRequestRefs = true,
  wrap: (children: ReactNode) => ReactNode = (children) => children,
): void {
  render(
    <QueryClientProvider client={client}>
      <FileViewerContext.Provider value={FILE_VIEWER}>
        {wrap(
          <FilePathAwareMessageResponse linkPullRequestRefs={linkPullRequestRefs}>
            {markdown}
          </FilePathAwareMessageResponse>,
        )}
      </FileViewerContext.Provider>
    </QueryClientProvider>,
  );
}

function hrefs(): string[] {
  return screen.queryAllByRole("link").map((link) => link.getAttribute("href") ?? "");
}

describe("pull request references in chat markdown", () => {
  it("links a bare reference to the session repo's PR", () => {
    seed(githubInfo());
    renderMarkdown("Landed in #4242.");

    const link = screen.getByRole("link", { name: /#4242/ });
    expect(link).toHaveAttribute("href", "https://github.com/acme/app/pull/4242");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("links an explicit owner/repo reference to that repo", () => {
    seed(githubInfo());
    renderMarkdown("Upstream: omnigent-ai/omnigent#9882");
    expect(hrefs()).toEqual(["https://github.com/omnigent-ai/omnigent/pull/9882"]);
  });

  it("leaves a bare reference as text when the session's repo is unknown", () => {
    seed(githubInfo({ repo: { name_with_owner: null, host: "github.com" } }));
    renderMarkdown("See #4242 and acme/app#12");
    expect(hrefs()).toEqual(["https://github.com/acme/app/pull/12"]);
    expect(screen.getByText("#4242")).toBeVisible();
  });

  it("links once the session's repo arrives after the message rendered", async () => {
    serveGithubInfo(() => Response.json(githubInfo()));
    renderMarkdown("See #4242");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("#4242")).toBeVisible();

    expect(await screen.findByRole("link", { name: /#4242/ })).toHaveAttribute(
      "href",
      "https://github.com/acme/app/pull/4242",
    );
  });

  it("leaves code, existing links, URL fragments and non-references alone", () => {
    seed(githubInfo());
    renderMarkdown(
      [
        "Inline `#1234` and [#5678](https://example.com/x) and https://example.com/page#4321.",
        "",
        "a#1234 file.md#L1234 #123 #0123 #fff #123abc",
        "",
        "```",
        "#1234",
        "```",
      ].join("\n"),
    );
    expect(hrefs()).toEqual(["https://example.com/x", "https://example.com/page#4321"]);
  });

  it("keeps references as text when the session isn't on GitHub", () => {
    seed(githubInfo({ provider: "gitlab" }));
    renderMarkdown("See #4242 and acme/app#12");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("#4242")).toBeVisible();
  });

  // pullRequestRefs.test.ts covers the other malformed names.
  it("keeps references as text for a malformed repo name", () => {
    seed(githubInfo({ repo: { name_with_owner: "javascript:alert(1)", host: "github.com" } }));
    renderMarkdown("See #4242");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("#4242")).toBeVisible();
  });

  it("keeps references as text when the session's GitHub info fails to load", async () => {
    serveGithubInfo(() => new Response("boom", { status: 500 }));
    renderMarkdown("See #4242 and acme/app#12");
    await waitFor(() =>
      expect(client.getQueryState(["github-info", CONVERSATION_ID])?.status).toBe("error"),
    );
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("#4242")).toBeVisible();
  });

  it("doesn't refetch failed GitHub info for each newly rendered message", async () => {
    serveGithubInfo(() => new Response("boom", { status: 500 }));
    renderMarkdown("See #4242");
    await waitFor(() =>
      expect(client.getQueryState(["github-info", CONVERSATION_ID])?.status).toBe("error"),
    );
    const calls = githubInfoRequests().length;
    renderMarkdown("And #4243");
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(githubInfoRequests()).toHaveLength(calls);
  });

  it("resolves against a side chat's own session, not the parent's", () => {
    seed(githubInfo());
    client.setQueryData(
      ["github-info", "conv_child"],
      githubInfo({ repo: { name_with_owner: "acme/lib", host: "github.com" } }),
    );
    renderMarkdown("See #4242", true, (children) => (
      <ConversationScopeContext.Provider value="conv_child">
        {children}
      </ConversationScopeContext.Provider>
    ));
    expect(hrefs()).toEqual(["https://github.com/acme/lib/pull/4242"]);
  });

  it("opens a side chat's link in the parent's in-app browser", async () => {
    nativeShell.native = true;
    const openOrNavigate = vi.fn().mockResolvedValue({ ok: true });
    Object.assign(window, {
      omnigentDesktop: { kind: "electron", browserOpenOrNavigate: openOrNavigate },
    });
    writeOpenLinksInApp(true);
    seed(githubInfo());
    client.setQueryData(
      ["github-info", "conv_child"],
      githubInfo({ repo: { name_with_owner: "acme/lib", host: "github.com" } }),
    );
    renderMarkdown("See #4242", true, (children) => (
      <ConversationScopeContext.Provider value="conv_child">
        {children}
      </ConversationScopeContext.Provider>
    ));

    const link = screen.getByRole("link", { name: /#4242/ });
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
    link.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    await vi.waitFor(() =>
      expect(openOrNavigate).toHaveBeenCalledWith(
        expect.stringMatching(new RegExp(`^browser-tab:${CONVERSATION_ID}:[^:]+$`)),
        "https://github.com/acme/lib/pull/4242",
        undefined,
        expect.objectContaining({ agent: true }),
      ),
    );
  });

  it("links nothing unless the caller opts in", () => {
    seed(githubInfo());
    renderMarkdown("See #4242", false);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    cleanup();
    render(
      <QueryClientProvider client={client}>
        <FileViewerContext.Provider value={FILE_VIEWER}>
          <FilePathAwareMessageResponse>See #4242</FilePathAwareMessageResponse>
        </FileViewerContext.Provider>
      </QueryClientProvider>,
    );
    expect(screen.getByText(/#4242/)).toBeVisible();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
