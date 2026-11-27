// Warm session switches: a sidebar-style click switches the store with the navigation,
// so the commit landing `/c/:id` skips the hydrating placeholder and the surface stays mounted.
import { forwardRef, Profiler, useLayoutEffect } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, MemoryRouter, Outlet, Route, Routes, useParams } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getBasePath, withBasePath } from "@/lib/basePath";
import {
  basenamedRouting,
  Link,
  reactRouterRouting,
  RoutingProvider,
  useRebasePath,
  type OmnigentLinkProps,
  type RoutingApi,
} from "@/lib/routing";
import { TooltipProvider } from "@/components/ui/tooltip";
import type * as HeaderConversationMenuModule from "@/shell/HeaderConversationMenu";
import { AppShell } from "@/shell/AppShell";
import { useCommandContext } from "@/shell/commandContext";
import { writeWorkspacePanelDefault } from "@/lib/workspacePanelPreferences";
import { Sidebar } from "@/shell/Sidebar";
import { conversation } from "@/test/sidebarMockHelpers";
import { sessionRowsPage } from "@/lib/sidebarData";
import type { UserMessageBlock } from "@/lib/blocks";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import * as capabilities from "@/lib/capabilities";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { switchSessionBeforeNavigate, isWarmSwitchPending } from "@/lib/warmSessionSwitch";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { conversationRegistry } from "@/store/conversationRegistry";
import {
  consumePendingInitialPrompt,
  initChatStore,
  setPendingInitialPrompt,
  useChatStore,
} from "@/store/chatStore";
import { clearSessionDrafts, getSessionDraft, setSessionDraft } from "@/lib/sessionDrafts";
import * as clipboard from "@/lib/clipboard";
import * as serverOrigin from "@/lib/serverOrigin";
import * as alwaysSteer from "@/lib/alwaysSteerPreferences";
import * as conversationSnapshot from "@/store/conversationSnapshot";
import { resetStreamSlotManager } from "@/store/streamSlots";
import * as sessionsApi from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { ChatPage } from "./ChatPage";

// Observe the committed menu destination without replacing its real actions.
vi.mock("@/shell/HeaderConversationMenu", async (importOriginal) => {
  const original = await importOriginal<typeof HeaderConversationMenuModule>();
  return {
    ...original,
    HeaderConversationMenu: (props: Parameters<typeof original.HeaderConversationMenu>[0]) => (
      <span data-header-menu-target={props.conversation.id}>
        <original.HeaderConversationMenu {...props} />
      </span>
    ),
  };
});

const initialStoreState = useChatStore.getState();

/** A committed user turn, so each conversation's transcript has visible text. */
function userBlock(id: string, text: string): UserMessageBlock {
  return {
    type: "user_message",
    ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: id, itemId: id },
    content: [{ type: "input_text", text }],
  };
}

/** A retained entry with an open stream — what a warm switch paints from. */
function seedLiveEntry(id: string, text: string) {
  const entry = conversationRegistry.acquire(id);
  entry.setState({
    abortController: new AbortController(),
    blocks: [userBlock(id, text)],
    boundAgentId: "agent_example",
  });
  return entry;
}

/** The chat surface's scroll container; a remount replaces the DOM node. */
const transcriptNode = () => document.querySelector(".session-chat-transcript");

interface CommitShape {
  urlConvId: string | undefined;
  activeConversationId: string | null;
  loadingConversation: boolean;
  commandConversationId?: string;
}
const commitLog: CommitShape[] = [];

/** Records what each commit of the route saw, so placeholder commits are observable. */
function RouteCommitLog() {
  const { conversationId: urlConvId } = useParams<{ conversationId: string }>();
  const activeConversationId = useChatStore((s) => s.conversationId);
  const loadingConversation = useChatStore((s) => s.loadingConversation);
  const command = useCommandContext();
  useLayoutEffect(() => {
    commitLog.push({
      urlConvId,
      activeConversationId,
      loadingConversation,
      commandConversationId: command.session?.id,
    });
  });
  return null;
}

/** The sidebar row's click shape: switch first, then let the Link navigate. */
function SessionLink({
  id,
  label,
  cancelNavigation = false,
  deferNavigation = false,
}: {
  id: string;
  label: string;
  cancelNavigation?: boolean;
  deferNavigation?: boolean;
}) {
  const rebasePath = useRebasePath();
  return (
    <Link
      to={`/c/${id}`}
      onClick={(event) => {
        switchSessionBeforeNavigate(id, rebasePath);
        if (cancelNavigation || deferNavigation) event.preventDefault();
        if (deferNavigation && !cancelNavigation) {
          window.history.pushState({}, "", withBasePath(rebasePath(`/c/${id}`)));
        }
      }}
    >
      {label}
    </Link>
  );
}

const CancelingHostLink = forwardRef<HTMLAnchorElement, OmnigentLinkProps>(
  function CancelingHostLink({ onClick, ...props }, ref) {
    const BaseLink = reactRouterRouting.Link;
    return (
      <BaseLink
        {...props}
        ref={ref}
        onClick={(event) => {
          onClick?.(event);
          event.preventDefault();
        }}
      />
    );
  },
);

function renderPage(
  qc = new QueryClient(),
  options: {
    cancelBetaNavigation?: boolean;
    deferNavigation?: boolean;
    initialConversationId?: string;
    info?: Partial<capabilities.ServerInfo>;
    routing?: RoutingApi;
    sidebar?: boolean;
    shell?: boolean;
    onCommit?: () => void;
  } = {},
) {
  const info = { ...FALLBACK_SERVER_INFO, ...options.info };
  const routing = options.routing ?? reactRouterRouting;
  const initialPath = withBasePath(
    routing.rebasePath(`/c/${options.initialConversationId ?? "conv_a"}`),
  );
  window.history.replaceState({}, "", initialPath);
  const Router = options.deferNavigation ? MemoryRouter : BrowserRouter;
  vi.spyOn(capabilities, "getCachedServerInfo").mockReturnValue(info);
  render(
    <QueryClientProvider client={qc}>
      <CapabilitiesProvider info={info}>
        <SidebarDataProvider>
          <Router initialEntries={[initialPath]} basename={getBasePath() || undefined}>
            <RoutingProvider value={routing}>
              <Routes>
                <Route
                  element={
                    options.shell ? (
                      <Profiler id="shell" onRender={options.onCommit ?? (() => {})}>
                        <TooltipProvider>
                          <AppShell />
                        </TooltipProvider>
                      </Profiler>
                    ) : (
                      <Outlet />
                    )
                  }
                >
                  <Route
                    path={routing.rebasePath("/c/:conversationId")}
                    element={
                      <>
                        <RouteCommitLog />
                        {options.sidebar && (
                          <TooltipProvider>
                            <Sidebar open onClose={() => {}} />
                          </TooltipProvider>
                        )}
                        <SessionLink
                          id="conv_a"
                          label="open alpha"
                          deferNavigation={options.deferNavigation}
                        />
                        <SessionLink
                          id="conv_b"
                          label="open beta"
                          cancelNavigation={options.cancelBetaNavigation}
                          deferNavigation={options.deferNavigation}
                        />
                        <SessionLink
                          id="conv_c"
                          label="open gamma"
                          deferNavigation={options.deferNavigation}
                        />
                        <Link to="/c/conv_a">finish alpha navigation</Link>
                        <Link to="/c/conv_b">finish beta navigation</Link>
                        <Link to="/c/conv_c">finish gamma navigation</Link>
                        <ChatPage />
                      </>
                    }
                  />
                </Route>
              </Routes>
            </RoutingProvider>
          </Router>
        </SidebarDataProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

function renderWarmComposer(
  options: {
    session?: Partial<Session>;
    info?: Partial<capabilities.ServerInfo>;
    routing?: RoutingApi;
    sidebar?: boolean;
  } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  initChatStore(qc);
  for (const id of ["conv_a", "conv_b"]) {
    seedLiveEntry(id, id === "conv_a" ? "alpha transcript" : "beta transcript").setState({
      sessionHarness: options.session?.harness ?? null,
    });
    qc.setQueryData(["session", id], {
      id,
      agentId: "agent_example",
      permissionLevel: 4,
      status: "idle",
      labels: {},
      ...options.session,
    });
  }
  useChatStore.setState({ boundAgentId: "agent_example" });
  renderPage(qc, {
    info: options.info,
    routing: options.routing,
    sidebar: options.sidebar,
  });
}

function headerSnapshot() {
  const breadcrumb = document.querySelector(".conversation-breadcrumb");
  const title = breadcrumb?.querySelector('[data-testid="header-title"], .breadcrumb-parent-link');
  const project = breadcrumb?.querySelector('[aria-label^="Project: "]');
  return {
    ...commitLog.at(-1),
    title: title?.textContent ?? null,
    project: project?.getAttribute("aria-label") ?? null,
    icon: project?.textContent ?? null,
    parentLink: breadcrumb?.querySelector(".breadcrumb-parent-link")?.getAttribute("href") ?? null,
    breadcrumb: breadcrumb?.textContent ?? null,
    menuTarget:
      breadcrumb
        ?.querySelector("[data-header-menu-target]")
        ?.getAttribute("data-header-menu-target") ?? null,
    transcript: transcriptNode()?.textContent ?? "",
  };
}

function renderHeaderPage(
  child: boolean,
  frames: ReturnType<typeof headerSnapshot>[],
  deferNavigation = true,
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  initChatStore(qc);
  seedLiveEntry("conv_a", "alpha transcript");
  seedLiveEntry("conv_b", "beta transcript");
  const rows = sessionRowsPage([
    conversation("conv_a", {
      title: "Alpha session",
      permission_level: 4,
      project_id: "project_a",
    }),
    conversation(child ? "conv_parent" : "conv_b", {
      title: child ? "Beta parent" : "Beta session",
      permission_level: 4,
      project_id: "project_b",
    }),
  ]);
  const projects = [
    { id: "project_a", name: "Alpha project", icon: "X" },
    { id: "project_b", name: "Beta project", icon: "Y" },
  ];
  qc.setQueryData(["projects"], projects);
  for (const id of ["conv_a", "conv_b", "conv_parent"]) {
    qc.setQueryData(["session", id], {
      id,
      agentId: `agent_${id}`,
      agentName: "Example agent",
      permissionLevel: 4,
      status: "idle",
      title: id === "conv_a" ? "Alpha session" : "Beta session",
      createdAt: 0,
      labels: {},
      items: [],
      pendingElicitations: [],
      parentSessionId: id === "conv_b" && child ? "conv_parent" : null,
      subAgentName: id === "conv_b" && child ? "Worker B" : null,
    });
    qc.setQueryData(["session-agent", id], {
      id: `agent_${id}`,
      name: "Example agent",
      mcp_servers: [],
      policies: [],
      terminals: [],
    });
  }
  useChatStore.setState({ boundAgentId: "agent_conv_a" });
  writeWorkspacePanelDefault("collapsed");
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (String(url).includes("/projects"))
        return Promise.resolve(new Response(JSON.stringify(projects)));
      if (String(url).includes("/v1/sessions?"))
        return Promise.resolve(new Response(JSON.stringify(rows)));
      return new Promise<Response>(() => {});
    }),
  );
  renderPage(qc, {
    shell: true,
    deferNavigation,
    onCommit: () => frames.push(headerSnapshot()),
  });
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState({}, "", "/c/conv_a");
  useChatStore.setState({
    conversationId: "conv_a",
    blocks: [userBlock("item_a", "alpha transcript")],
    loadingConversation: false,
    conversationLoadError: null,
  });
});

afterEach(() => {
  cleanup();
  window.history.replaceState({}, "", "/");
  window.getSelection()?.removeAllRanges();
  useChatStore.getState().abortController?.abort();
  conversationRegistry.clear();
  clearSessionDrafts();
  localStorage.clear();
  sessionStorage.clear();
  consumePendingInitialPrompt("conv_a");
  useChatStore.setState(initialStoreState);
  commitLog.length = 0;
  if (vi.isFakeTimers()) vi.clearAllTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("ChatPage warm switch", () => {
  it("live target: the surface stays mounted and no placeholder renders", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    seedLiveEntry("conv_b", "beta transcript");
    renderPage(qc);
    await waitFor(() => expect(document.body.textContent).toContain("alpha transcript"));
    const surfaceBefore = transcriptNode();
    expect(surfaceBefore).not.toBeNull();
    commitLog.length = 0;

    act(() => fireEvent.click(screen.getByRole("link", { name: "open beta" })));

    expect(useChatStore.getState().conversationId).toBe("conv_b");
    expect(screen.queryByText("Loading conversation…")).toBeNull();
    expect(transcriptNode()).toBe(surfaceBefore);
    await waitFor(() => expect(document.body.textContent).toContain("beta transcript"));
    // The store write re-renders one frame before the URL's transition commit
    // (external-store updates are synchronous); the gate holds the surface
    // through that window, so every commit after the click shows the target.
    expect(commitLog.every((commit) => commit.activeConversationId === "conv_b")).toBe(true);
    expect(commitLog).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ urlConvId: "conv_b", activeConversationId: "conv_b" }),
      ]),
    );
  });

  it.each([false, true])(
    "uses the host routing prefix for the real sidebar and chat (canceled: %s)",
    async (canceled) => {
      const basename = "/ml/omnigent-embed";
      const rows = sessionRowsPage([conversation("conv_a"), conversation("conv_b")]);
      vi.stubGlobal(
        "fetch",
        vi.fn((url: string) => {
          if (String(url).includes("/projects")) return Promise.resolve(new Response("[]"));
          if (String(url).includes("/v1/sessions?"))
            return Promise.resolve(new Response(JSON.stringify(rows)));
          return new Promise<Response>(() => {});
        }),
      );
      const routing = basenamedRouting(
        basename,
        canceled ? { ...reactRouterRouting, Link: CancelingHostLink } : reactRouterRouting,
      );
      renderWarmComposer({ routing, sidebar: true });
      const row = await screen.findByText("conv_b");
      await screen.findByText("alpha transcript");
      const surface = transcriptNode();
      const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");
      vi.useFakeTimers();

      fireEvent.click(row);
      expect(useChatStore.getState().conversationId).toBe("conv_b");
      if (!canceled) {
        expect(screen.getByText("beta transcript")).toBeInTheDocument();
        expect(screen.queryByText("Loading conversation…")).toBeNull();
        expect(transcriptNode()).toBe(surface);
      }
      await act(() => vi.advanceTimersByTimeAsync(0));

      const target = canceled ? "conv_a" : "conv_b";
      expect(useChatStore.getState().conversationId).toBe(target);
      expect(window.location.pathname).toBe(`${basename}/c/${target}`);
      expect(commitLog.at(-1)).toMatchObject({ urlConvId: target, activeConversationId: target });
      expect(screen.queryByText("Loading conversation…")).toBeNull();
      if (canceled) expect(screen.getByText("alpha transcript")).toBeInTheDocument();
      else {
        expect(transcriptNode()).toBe(surface);
        expect(switchTo.mock.calls).not.toContainEqual(["conv_a"]);
      }
    },
  );

  it.each([false, true])(
    "updates the header with the transcript in one pending commit (child: %s)",
    async (child) => {
      const frames: ReturnType<typeof headerSnapshot>[] = [];
      renderHeaderPage(child, frames);
      await screen.findByRole("button", { name: "Project: Alpha project" });
      await screen.findByText("alpha transcript");
      frames.length = 0;

      fireEvent.click(screen.getByRole("link", { name: "open beta" }));

      const expected = child ? "Beta parent" : "Beta session";
      const pending = frames.filter(
        (frame) => frame.urlConvId === "conv_a" && frame.activeConversationId === "conv_b",
      );
      expect(pending.length).toBeGreaterThan(0);
      for (const frame of pending) {
        expect(frame.transcript).toContain("beta transcript");
        expect(frame.title).toBe(expected);
        expect(frame.project).toBe("Project: Beta project");
        expect(frame.icon).toContain("Y");
        expect(frame.menuTarget).toBe(child ? null : "conv_b");
        expect(frame.parentLink).toBe(child ? "/c/conv_parent" : null);
        expect(frame.commandConversationId).toBe("conv_a");
        if (child) expect(frame.breadcrumb).toContain("Worker B");
      }
      fireEvent.click(screen.getByRole("link", { name: "finish beta navigation" }));
      expect(headerSnapshot()).toMatchObject({
        title: expected,
        project: "Project: Beta project",
        menuTarget: child ? null : "conv_b",
        parentLink: child ? "/c/conv_parent" : null,
        urlConvId: "conv_b",
        activeConversationId: "conv_b",
        commandConversationId: "conv_b",
      });
      expect(
        frames.filter(
          (frame) => frame.transcript.includes("beta transcript") && frame.title !== expected,
        ),
      ).toEqual([]);
    },
  );

  it.each(["share", "fork"] as const)(
    "pins the pending header's %s dialog to B",
    async (action) => {
      vi.spyOn(serverOrigin, "isCurrentServerLocal").mockReturnValue(false);
      const copy = vi.spyOn(clipboard, "copyText").mockResolvedValue(undefined);
      const frames: ReturnType<typeof headerSnapshot>[] = [];
      renderHeaderPage(false, frames);
      await screen.findByRole("button", { name: "Project: Alpha project" });
      fireEvent.click(screen.getByRole("link", { name: "open beta" }));

      if (action === "share") {
        fireEvent.click(screen.getByRole("button", { name: "Share session" }));
        await screen.findByRole("dialog", { name: "Share this session" });
        fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
        await waitFor(() =>
          expect(copy).toHaveBeenCalledWith(expect.stringContaining("/c/conv_b")),
        );
      } else {
        fireEvent.keyDown(screen.getByTestId("header-conversation-actions"), { key: "ArrowDown" });
        fireEvent.click(screen.getByTestId("header-fork-conversation"));
        await screen.findByTestId("fork-session-dialog");
        fireEvent.click(screen.getByTestId("fork-session-advanced-toggle"));
        expect(await screen.findByPlaceholderText("Fork of Beta session")).toBeInTheDocument();
      }
      fireEvent.click(screen.getByText("finish beta navigation"));
      expect(headerSnapshot().urlConvId).toBe("conv_b");
      if (action === "share")
        expect(screen.getByRole("dialog", { name: "Share this session" })).toBeInTheDocument();
      else expect(screen.getByPlaceholderText("Fork of Beta session")).toBeInTheDocument();
    },
  );

  it("keeps header navigation URL-owned on Back and Forward", async () => {
    const frames: ReturnType<typeof headerSnapshot>[] = [];
    renderHeaderPage(false, frames, false);
    await screen.findByRole("button", { name: "Project: Alpha project" });
    fireEvent.click(screen.getByRole("link", { name: "open beta" }));
    expect(headerSnapshot().title).toBe("Beta session");

    act(() => window.history.back());
    await waitFor(() => expect(useChatStore.getState().conversationId).toBe("conv_a"));
    expect(headerSnapshot()).toMatchObject({
      title: "Alpha session",
      menuTarget: "conv_a",
      urlConvId: "conv_a",
    });
    act(() => window.history.forward());
    await waitFor(() => expect(useChatStore.getState().conversationId).toBe("conv_b"));
    expect(headerSnapshot()).toMatchObject({
      title: "Beta session",
      menuTarget: "conv_b",
      urlConvId: "conv_b",
    });
  });

  it("cold target still shows the placeholder", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    // conv_c was never opened: no live entry, so the switch must cold-bind and
    // keep the hydrating placeholder until its snapshot lands. The never-settling
    // fetch pins that state for the assertion.
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    renderPage(qc);
    await waitFor(() => expect(document.body.textContent).toContain("alpha transcript"));
    const surfaceBefore = transcriptNode();

    act(() => fireEvent.click(screen.getByRole("link", { name: "open gamma" })));

    expect(useChatStore.getState().conversationId).toBe("conv_c");
    expect(useChatStore.getState().loadingConversation).toBe(true);
    expect(screen.getByText("Loading conversation…")).toBeInTheDocument();
    expect(transcriptNode()).not.toBe(surfaceBefore);
  });

  it("restores a canceled navigation on the next task", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    seedLiveEntry("conv_b", "beta transcript");
    renderPage(qc, { cancelBetaNavigation: true });
    await screen.findByText("alpha transcript");
    vi.useFakeTimers();

    fireEvent.click(screen.getByRole("link", { name: "open beta" }));
    expect(useChatStore.getState().conversationId).toBe("conv_b");
    window.history.replaceState({}, "", "/c/conv_a?diff=1");
    await act(() => vi.advanceTimersByTimeAsync(0));

    expect(useChatStore.getState().conversationId).toBe("conv_a");
    expect(screen.getByText("alpha transcript")).toBeInTheDocument();
    expect(screen.queryByText("Loading conversation…")).toBeNull();
    expect(window.location.pathname).toBe("/c/conv_a");
  });

  it("keeps the target mounted across an arbitrarily slow router commit", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    seedLiveEntry("conv_b", "beta transcript");
    renderPage(qc, { deferNavigation: true });
    await screen.findByText("alpha transcript");
    const surface = transcriptNode();
    vi.useFakeTimers();
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");

    fireEvent.click(screen.getByRole("link", { name: "open beta" }));
    await act(() => vi.advanceTimersByTimeAsync(5000));
    act(() => useChatStore.setState({ backgroundTaskCount: 1 }));

    expect(commitLog.at(-1)).toMatchObject({ urlConvId: "conv_a", activeConversationId: "conv_b" });
    expect(useChatStore.getState().conversationId).toBe("conv_b");
    expect(screen.getByText("beta transcript")).toBeInTheDocument();
    expect(transcriptNode()).toBe(surface);
    expect(screen.queryByText("Loading conversation…")).toBeNull();
    expect(switchTo.mock.calls).not.toContainEqual(["conv_a"]);
    fireEvent.click(screen.getByRole("link", { name: "finish beta navigation" }));
    expect(commitLog.at(-1)).toMatchObject({ urlConvId: "conv_b", activeConversationId: "conv_b" });
    expect(transcriptNode()).toBe(surface);
  });

  it("keeps C mounted through two warm clicks before the router commits", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    seedLiveEntry("conv_b", "beta transcript");
    seedLiveEntry("conv_c", "gamma transcript");
    renderPage(qc, { deferNavigation: true });
    await screen.findByText("alpha transcript");
    const surface = transcriptNode();
    const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");
    vi.useFakeTimers();

    act(() => {
      fireEvent.click(screen.getByRole("link", { name: "open beta" }));
      fireEvent.click(screen.getByRole("link", { name: "open gamma" }));
    });
    await act(() => vi.advanceTimersByTimeAsync(0));

    expect(commitLog.at(-1)).toMatchObject({ urlConvId: "conv_a", activeConversationId: "conv_c" });
    expect(useChatStore.getState().conversationId).toBe("conv_c");
    expect(window.location.pathname).toBe("/c/conv_c");
    expect(screen.getByText("gamma transcript")).toBeInTheDocument();
    expect(screen.queryByText("Loading conversation…")).toBeNull();
    expect(transcriptNode()).toBe(surface);
    expect(switchTo.mock.calls).toEqual([["conv_b"], ["conv_c"]]);
    fireEvent.click(screen.getByRole("link", { name: "finish gamma navigation" }));
    expect(commitLog.at(-1)).toMatchObject({ urlConvId: "conv_c", activeConversationId: "conv_c" });
    expect(transcriptNode()).toBe(surface);
  });

  it("keeps the normal placeholder boundary for Back and Forward between live sessions", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    seedLiveEntry("conv_b", "beta transcript");
    renderPage(qc);
    await screen.findByText("alpha transcript");
    fireEvent.click(screen.getByRole("link", { name: "open beta" }));
    const betaSurface = transcriptNode();
    commitLog.length = 0;

    act(() => window.history.back());
    await waitFor(() => expect(useChatStore.getState().conversationId).toBe("conv_a"));
    expect(commitLog).toContainEqual(
      expect.objectContaining({ urlConvId: "conv_a", activeConversationId: "conv_b" }),
    );
    expect(transcriptNode()).not.toBe(betaSurface);
    const alphaSurface = transcriptNode();
    commitLog.length = 0;

    act(() => window.history.forward());
    await waitFor(() => expect(useChatStore.getState().conversationId).toBe("conv_b"));
    expect(commitLog).toContainEqual(
      expect.objectContaining({ urlConvId: "conv_b", activeConversationId: "conv_a" }),
    );
    expect(transcriptNode()).not.toBe(alphaSurface);
  });

  it.each([null, { name: "example-skill", args: "check" }])(
    "holds cold A's initial prompt across a warm switch and pins delivery on return: %o",
    async (skill) => {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      initChatStore(qc);
      const alpha = seedLiveEntry("conv_a", "alpha transcript");
      seedLiveEntry("conv_b", "beta transcript");
      alpha.setState({ loadingConversation: true });
      const send = vi.fn().mockResolvedValue(undefined);
      const sendSlashCommand = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ send, sendSlashCommand, loadingConversation: true });
      setPendingInitialPrompt("conv_a", { text: "alpha private prompt", skill });
      renderPage(qc);
      expect(screen.getByText("Loading conversation…")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("link", { name: "open beta" }));

      expect(screen.getByText("beta transcript")).toBeInTheDocument();
      expect(send).not.toHaveBeenCalled();
      expect(sendSlashCommand).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("link", { name: "open alpha" }));
      expect(send).not.toHaveBeenCalled();
      expect(sendSlashCommand).not.toHaveBeenCalled();
      act(() => alpha.setState({ loadingConversation: false }));

      await waitFor(() => expect(skill ? sendSlashCommand : send).toHaveBeenCalledTimes(1));
      if (skill) {
        expect(sendSlashCommand).toHaveBeenCalledWith(skill.name, skill.args, "agent_example", {
          pinnedConversationId: "conv_a",
        });
        expect(send).not.toHaveBeenCalled();
      } else {
        expect(send).toHaveBeenCalledWith("alpha private prompt", "agent_example", [], {
          pinnedConversationId: "conv_a",
        });
        expect(sendSlashCommand).not.toHaveBeenCalled();
      }
      fireEvent.click(screen.getByRole("link", { name: "open beta" }));
      fireEvent.click(screen.getByRole("link", { name: "open alpha" }));
      expect(skill ? sendSlashCommand : send).toHaveBeenCalledTimes(1);
    },
  );

  it("clears command and attachment errors without remounting the transcript", async () => {
    renderWarmComposer();
    let rejectModel!: (error: Error) => void;
    const setModel = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectModel = reject;
        }),
    );
    useChatStore.setState({ setModel });
    const input = await screen.findByRole("textbox");
    fireEvent.change(input, { target: { value: "/model example-model" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(setModel).toHaveBeenCalledOnce();
    fireEvent.change(input, { target: { value: "/new unexpected" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByText("/new does not accept arguments")).toBeInTheDocument();
    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files: [new File(["audio"], "recording.wav", { type: "audio/wav" })] },
    });
    expect(screen.getByText(/can't be attached/)).toBeInTheDocument();
    const surfaceBefore = transcriptNode();

    fireEvent.click(screen.getByRole("link", { name: "open beta" }));

    expect(transcriptNode()).toBe(surfaceBefore);
    expect(screen.queryByText("/new does not accept arguments")).toBeNull();
    expect(screen.queryByText(/can't be attached/)).toBeNull();
    await act(async () => rejectModel(new Error("alpha model update failed")));
    expect(screen.queryByText("alpha model update failed")).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: "open alpha" }));
    expect(screen.queryByText("alpha model update failed")).toBeNull();
  });

  it("stops dictation at a warm switch so late transcripts and Escape cannot replace B's draft", () => {
    const recognizers: Recognition[] = [];
    class Recognition extends EventTarget {
      lang = "en-US";
      continuous = false;
      interimResults = false;
      start = vi.fn(() => this.dispatchEvent(new Event("start")));
      stop = vi.fn();
      constructor() {
        super();
        recognizers.push(this);
      }
    }
    vi.stubGlobal("SpeechRecognition", Recognition);
    setSessionDraft("conv_b", { text: "beta draft", files: [] });
    renderWarmComposer();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "alpha draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    const recognition = recognizers[0]!;
    const surface = transcriptNode();

    fireEvent.click(screen.getByRole("link", { name: "open beta" }));

    expect(recognition.stop).toHaveBeenCalled();
    expect(transcriptNode()).toBe(surface);
    act(() =>
      recognition.dispatchEvent(
        Object.assign(new Event("result"), {
          resultIndex: 0,
          results: [Object.assign([{ transcript: "late alpha words" }], { isFinal: true })],
        }),
      ),
    );
    expect(screen.getByRole("textbox")).toHaveValue("beta draft");
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" });
    expect(screen.getByRole("textbox")).toHaveValue("beta draft");
    expect(getSessionDraft("conv_b")?.text).toBe("beta draft");
    fireEvent.click(screen.getByRole("link", { name: "open alpha" }));
    expect(screen.getByRole("textbox")).toHaveValue("alpha draft");
  });

  it("does not show A's deferred rewind failure in B", async () => {
    vi.spyOn(sessionsApi, "fetchRewindPrompts").mockResolvedValue({
      prompts: [{ itemId: "item_a", text: "alpha prompt" }],
      olderCursor: null,
    });
    let rejectRewind!: (error: Error) => void;
    const rewind = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectRewind = reject;
        }),
    );
    useChatStore.setState({ rewindToPrompt: rewind });
    renderWarmComposer({ session: { harness: "claude-sdk" }, info: { session_rewind: true } });
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "/rewind" } });
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    fireEvent.click(await screen.findByTestId("rewind-prompt"));
    expect(rewind).toHaveBeenCalledWith("item_a");

    fireEvent.click(screen.getByRole("link", { name: "open beta" }));
    await act(async () => rejectRewind(new Error("alpha rewind failed")));

    expect(screen.getByText("beta transcript")).toBeInTheDocument();
    expect(screen.queryByText("alpha rewind failed")).toBeNull();
  });

  it("starts B's permission and Plan controls idle while A's requests are pending", async () => {
    const permissionRequests: (() => void)[] = [];
    const setPermission = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          permissionRequests.push(resolve);
        }),
    );
    let finishPlan!: () => void;
    const setPlan = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finishPlan = resolve;
        }),
    );
    useChatStore.setState({ setCodexApprovalMode: setPermission, setCodexPlanMode: setPlan });
    renderWarmComposer({
      session: { harness: "codex-native", labels: { "omnigent.wrapper": "codex-native-ui" } },
    });
    fireEvent.keyDown(screen.getByRole("button", { name: "Add" }), { key: "ArrowDown" });
    fireEvent.click(screen.getByTestId("composer-plan-action"));
    expect(setPlan).toHaveBeenCalledOnce();
    fireEvent.keyDown(screen.getByTestId("composer-permission-chip"), { key: "ArrowDown" });
    fireEvent.click(await screen.findByTestId("composer-permission-option-full-access"));
    expect(setPermission).toHaveBeenCalledOnce();
    expect(screen.getByTestId("composer-permission-chip")).toBeDisabled();

    fireEvent.click(screen.getByRole("link", { name: "open beta" }));

    expect(screen.getByTestId("composer-permission-chip")).toBeEnabled();
    fireEvent.keyDown(screen.getByRole("button", { name: "Add" }), { key: "ArrowDown" });
    expect(screen.getByTestId("composer-plan-action")).not.toHaveAttribute("aria-disabled", "true");
    fireEvent.keyDown(screen.getByTestId("composer-plan-action"), { key: "Escape" });
    fireEvent.keyDown(screen.getByTestId("composer-permission-chip"), { key: "ArrowDown" });
    fireEvent.click(await screen.findByTestId("composer-permission-option-full-access"));
    expect(setPermission).toHaveBeenCalledTimes(2);
    await act(async () => {
      permissionRequests[0]!();
      finishPlan();
    });
    expect(screen.getByTestId("composer-permission-chip")).toBeDisabled();
    await act(async () => permissionRequests[1]!());
    expect(screen.getByTestId("composer-permission-chip")).toBeEnabled();
  });

  it("keeps the composer and typed draft mounted through temp-to-real promotion", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    const temporaryId = "temp:example";
    seedLiveEntry(temporaryId, "initial transcript");
    useChatStore.setState({ conversationId: temporaryId, boundAgentId: "agent_example" });
    renderPage(qc, {
      initialConversationId: temporaryId,
      deferNavigation: true,
    });
    const input = screen.getByRole("textbox");
    const surface = transcriptNode();
    fireEvent.change(input, { target: { value: "typing during session creation" } });

    act(() => {
      window.history.pushState({}, "", "/c/conv_a");
      useChatStore.setState({ conversationId: "conv_a" });
    });
    expect(isWarmSwitchPending("conv_a", temporaryId, reactRouterRouting.rebasePath)).toBe(true);
    expect(screen.getByRole("textbox")).toBe(input);
    fireEvent.click(screen.getByRole("link", { name: "finish alpha navigation" }));

    expect(screen.getByRole("textbox")).toBe(input);
    expect(input).toHaveValue("typing during session creation");
    expect(transcriptNode()).toBe(surface);
    expect(getSessionDraft("conv_a")?.text).toBe("typing during session creation");
    expect(getSessionDraft(temporaryId)).toBeUndefined();
  });

  it("treats a sidebar click away from a temporary session as a new composer, not promotion", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    const temporaryId = "temp:example";
    seedLiveEntry(temporaryId, "initial transcript");
    seedLiveEntry("conv_b", "beta transcript");
    setSessionDraft("conv_b", { text: "beta draft", files: [] });
    useChatStore.setState({ conversationId: temporaryId, boundAgentId: "agent_example" });
    renderPage(qc, { initialConversationId: temporaryId });
    const input = screen.getByRole("textbox");
    const surface = transcriptNode();
    fireEvent.change(input, { target: { value: "temporary draft" } });

    fireEvent.click(screen.getByRole("link", { name: "open beta" }));

    expect(screen.getByRole("textbox")).not.toBe(input);
    expect(screen.getByRole("textbox")).toHaveValue("beta draft");
    expect(transcriptNode()).not.toBe(surface);
    expect(getSessionDraft(temporaryId)?.text).toBe("temporary draft");
  });

  it("drops the outgoing session's selection popup while retaining the transcript", () => {
    renderWarmComposer();
    const surface = transcriptNode();
    const range = document.createRange();
    range.selectNodeContents(screen.getByText("alpha transcript"));
    range.getBoundingClientRect = () => new DOMRect(10, 10, 100, 20);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
    fireEvent(document, new Event("selectionchange"));
    expect(screen.getByRole("button", { name: "Reply ↵" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "open beta" }));

    expect(screen.queryByRole("button", { name: "Reply ↵" })).toBeNull();
    expect(transcriptNode()).toBe(surface);
  });
});

const raceIds = ["conv_a", "conv_b", "conv_c"] as const;
type RaceId = (typeof raceIds)[number];

function raceRandom(seed: number) {
  let state = seed;
  return (limit: number) => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return (state >>> 0) % limit;
  };
}

async function nextTask() {
  await act(async () => {
    await new Promise<void>((resolve) => {
      window.setTimeout(resolve, 0);
    });
  });
}

async function runSwitchRace(seed: number) {
  resetStreamSlotManager();
  vi.spyOn(alwaysSteer, "readAlwaysSteer").mockReturnValue(false);
  const random = raceRandom(seed);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  initChatStore(qc);
  for (const id of ["conv_a", "conv_b"] as const) {
    seedLiveEntry(id, `${id} transcript`);
  }
  for (const id of raceIds) {
    qc.setQueryData(["session", id], {
      id,
      agentId: "agent_example",
      agentName: "Example agent",
      permissionLevel: 4,
      status: "idle",
      labels: {},
    });
  }
  const send = vi.fn().mockResolvedValue(undefined);
  const enqueue = vi.fn();
  useChatStore.setState({
    blocks: [userBlock("conv_a", "conv_a transcript")],
    boundAgentId: "agent_example",
    send,
    enqueueMessage: enqueue,
  });
  const rows = sessionRowsPage(raceIds.map((id) => conversation(id)));
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, options?: RequestInit) => {
      if (String(url).includes("/projects")) return Promise.resolve(new Response("[]"));
      if (String(url).includes("/v1/sessions?"))
        return Promise.resolve(new Response(JSON.stringify(rows)));
      return new Promise<Response>((_resolve, reject) => {
        options?.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("Aborted", "AbortError")),
          { once: true },
        );
      });
    }),
  );
  let finishCold: (() => void) | undefined;
  vi.spyOn(conversationSnapshot, "fetchConversationSnapshot").mockImplementation(
    (_client, id) =>
      new Promise((resolve) => {
        finishCold = () =>
          resolve({
            session: {
              id,
              agentId: "agent_example",
              agentName: "Example agent",
              permissionLevel: 4,
              status: "idle",
              labels: {},
            } as Session,
            page: {
              items: [
                {
                  id: `item_${id}`,
                  response_id: `response_${id}`,
                  type: "message",
                  role: "user",
                  status: "completed",
                  content: [{ type: "input_text", text: `${id} transcript` }],
                },
              ],
              hasMore: false,
            },
          });
      }),
  );
  const routing = seed % 2 ? reactRouterRouting : basenamedRouting("/embed");
  renderPage(qc, {
    routing,
    sidebar: true,
    cancelBetaNavigation: true,
  });
  await screen.findByText("conv_a");
  await screen.findByText("conv_a transcript");
  const rowsById = Object.fromEntries(raceIds.map((id) => [id, screen.getByText(id)])) as Record<
    RaceId,
    HTMLElement
  >;
  const cancelBeta = screen.getByRole("link", { name: "open beta" });
  const input = () => document.querySelector("textarea")!;
  const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");
  const drafts: Record<RaceId, string> = { conv_a: "", conv_b: "", conv_c: "" };
  const history: RaceId[] = ["conv_a"];
  let index = 0;
  let current: RaceId = "conv_a";
  let coldReady = false;
  const trace: string[] = [];

  function recordNavigation(target: RaceId) {
    if (target === current) return;
    history.splice(index + 1);
    history.push(target);
    index = history.length - 1;
    current = target;
  }

  function check() {
    expect(window.location.pathname).toBe(routing.rebasePath(`/c/${current}`));
    expect(useChatStore.getState().conversationId).toBe(current);
    expect(transcriptNode()?.textContent).toContain(`${current} transcript`);
    expect(document.body.textContent).not.toContain("Loading conversation…");
    expect(input()).toHaveValue(drafts[current]);
    for (const id of raceIds) expect(getSessionDraft(id)?.text ?? "").toBe(drafts[id]);
  }

  async function click(target: RaceId, cold = false) {
    const source = current;
    const surface = transcriptNode();
    const firstCall = switchTo.mock.calls.length;
    fireEvent.click(rowsById[target]);
    recordNavigation(target);
    if (cold) {
      expect(document.body.textContent).toContain("Loading conversation…");
      await waitFor(() => expect(finishCold).toBeDefined());
      await act(async () => finishCold?.());
      finishCold = undefined;
      await waitFor(() => expect(transcriptNode()?.textContent).toContain(`${target} transcript`));
    } else if (source !== target) {
      expect(document.body.textContent).not.toContain("Loading conversation…");
      expect(transcriptNode()).toBe(surface);
    }
    await nextTask();
    if (source !== target)
      expect(switchTo.mock.calls.slice(firstCall)).not.toContainEqual([source]);
  }

  async function coldClick() {
    await click("conv_c", !coldReady);
    coldReady = true;
  }

  async function traverse(direction: "back" | "forward") {
    const next = index + (direction === "back" ? -1 : 1);
    if (next < 0 || next >= history.length) return;
    await act(async () => {
      const popped = new Promise<void>((resolve) => {
        window.addEventListener("popstate", () => resolve(), { once: true });
      });
      window.history[direction]();
      await popped;
    });
    index = next;
    current = history[index]!;
    await nextTask();
  }

  function typeDraft(text: string) {
    fireEvent.change(input(), { target: { value: text } });
    drafts[current] = text;
  }

  async function submit(queue: boolean, step: number) {
    if (!drafts[current]) typeDraft(`draft ${seed} ${step} for ${current}`);
    const text = drafts[current];
    const sendCount = send.mock.calls.length;
    const queueCount = enqueue.mock.calls.length;
    act(() => useChatStore.setState({ sessionStatus: queue ? "running" : "idle" }));
    fireEvent.keyDown(input(), { key: "Enter" });
    if (queue) {
      expect(enqueue).toHaveBeenCalledTimes(queueCount + 1);
      expect(enqueue.mock.calls.at(-1)?.[0]).toBe(text);
      expect(enqueue.mock.calls.at(-1)?.[3]).toBe(current);
      expect(send).toHaveBeenCalledTimes(sendCount);
    } else {
      expect(send).toHaveBeenCalledTimes(sendCount + 1);
      expect(send.mock.calls.at(-1)?.[0]).toBe(text);
      expect(send.mock.calls.at(-1)?.[3]).toMatchObject({ pinnedConversationId: current });
      expect(enqueue).toHaveBeenCalledTimes(queueCount);
    }
    drafts[current] = "";
    act(() => useChatStore.setState({ sessionStatus: "idle" }));
    await nextTask();
  }

  try {
    check();
    const actions = [
      "live",
      "cold",
      "active",
      "modified",
      "back",
      "forward",
      "draft",
      "send",
      "queue",
      "cancel",
      "double",
    ];
    const steps = 30;
    const shuffled = [...actions];
    for (let shuffleIndex = shuffled.length - 1; shuffleIndex > 0; shuffleIndex--) {
      const other = random(shuffleIndex + 1);
      [shuffled[shuffleIndex], shuffled[other]] = [shuffled[other]!, shuffled[shuffleIndex]!];
    }
    const runAction = async (step: number) => {
      const action = shuffled[step] ?? actions[random(actions.length)]!;
      trace.push(`${step}:${action}:${current}`);
      if (action === "live") {
        const targets = raceIds.filter((id) => id !== current && (coldReady || id !== "conv_c"));
        await click(targets[random(targets.length)]!);
      } else if (action === "cold") {
        await coldClick();
      } else if (action === "active") {
        await click(current);
      } else if (action === "modified") {
        const calls = switchTo.mock.calls.length;
        window.addEventListener("click", (event) => event.preventDefault(), { once: true });
        fireEvent.click(rowsById[raceIds[random(raceIds.length)]!], { metaKey: true });
        await nextTask();
        expect(switchTo).toHaveBeenCalledTimes(calls);
      } else if (action === "back" || action === "forward") {
        if (action === "back" && index === 0) {
          await click(current === "conv_a" ? "conv_b" : "conv_a");
        }
        if (action === "forward" && index === history.length - 1) {
          await click(current === "conv_a" ? "conv_b" : "conv_a");
          await traverse("back");
        }
        await traverse(action);
      } else if (action === "draft") {
        typeDraft(`draft ${seed} ${step} for ${current}`);
      } else if (action === "send" || action === "queue") {
        await submit(action === "queue", step);
      } else if (action === "cancel") {
        if (useChatStore.getState().conversationId === "conv_b") await click("conv_a");
        const source = current;
        fireEvent.click(cancelBeta);
        await nextTask();
        expect(useChatStore.getState().conversationId).toBe(source);
      } else if (action === "double") {
        if (!coldReady) await coldClick();
        const targets = raceIds.filter((id) => id !== current);
        const first = targets[random(targets.length)]!;
        const second = targets.find((id) => id !== first)!;
        const source = current;
        const surface = transcriptNode();
        const firstCall = switchTo.mock.calls.length;
        act(() => {
          fireEvent.click(rowsById[first]);
          fireEvent.click(rowsById[second]);
        });
        recordNavigation(first);
        recordNavigation(second);
        await nextTask();
        expect(transcriptNode()).toBe(surface);
        expect(switchTo.mock.calls.slice(firstCall)).not.toContainEqual([source]);
      }
      check();
    };
    // Steps depend on the previous one, so chain them (no-await-in-loop forbids awaiting in the loop).
    let sequence = Promise.resolve();
    for (let step = 0; step < steps; step++) sequence = sequence.then(() => runAction(step));
    await sequence;
  } catch (error) {
    throw new Error(`warm switch race seed ${seed}, ${trace.join(" ")}`, {
      cause: error,
    });
  } finally {
    qc.clear();
  }
}

// The first steps and the last one are fixed so every seed sends, queues and commits.
const fixedPendingSteps: Record<number, string> = {
  0: "switch",
  1: "send",
  2: "queue",
  29: "finish",
};

async function runPendingRace(seed: number) {
  resetStreamSlotManager();
  vi.spyOn(alwaysSteer, "readAlwaysSteer").mockReturnValue(false);
  const random = raceRandom(seed);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  initChatStore(qc);
  for (const id of raceIds) {
    seedLiveEntry(id, `${id} transcript`);
    qc.setQueryData(["session", id], {
      id,
      agentId: "agent_example",
      permissionLevel: 4,
      status: "idle",
      labels: {},
    });
  }
  const send = vi.fn().mockResolvedValue(undefined);
  const enqueue = vi.fn();
  useChatStore.setState({
    blocks: [userBlock("conv_a", "conv_a transcript")],
    boundAgentId: "agent_example",
    send,
    enqueueMessage: enqueue,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => {})),
  );
  const routing = seed % 2 ? reactRouterRouting : basenamedRouting("/embed");
  renderPage(qc, { routing, deferNavigation: true });
  await screen.findByText("conv_a transcript");
  const open = {
    conv_a: screen.getByRole("link", { name: "open alpha" }),
    conv_b: screen.getByRole("link", { name: "open beta" }),
    conv_c: screen.getByRole("link", { name: "open gamma" }),
  };
  const finish = {
    conv_a: screen.getByRole("link", { name: "finish alpha navigation" }),
    conv_b: screen.getByRole("link", { name: "finish beta navigation" }),
    conv_c: screen.getByRole("link", { name: "finish gamma navigation" }),
  };
  const input = () => document.querySelector("textarea")!;
  const switchTo = vi.spyOn(useChatStore.getState(), "switchTo");
  const surface = transcriptNode();
  const drafts: Record<RaceId, string> = { conv_a: "", conv_b: "", conv_c: "" };
  let router: RaceId = "conv_a";
  let visible: RaceId = "conv_a";
  const trace: string[] = [];

  function check() {
    expect(window.location.pathname).toBe(routing.rebasePath(`/c/${visible}`));
    expect(commitLog.at(-1)).toMatchObject({ urlConvId: router, activeConversationId: visible });
    expect(useChatStore.getState().conversationId).toBe(visible);
    expect(isWarmSwitchPending(visible, router, routing.rebasePath)).toBe(visible !== router);
    expect(transcriptNode()).toBe(surface);
    expect(transcriptNode()?.textContent).toContain(`${visible} transcript`);
    expect(document.body.textContent).not.toContain("Loading conversation…");
    expect(input()).toHaveValue(drafts[visible]);
    for (const id of raceIds) expect(getSessionDraft(id)?.text ?? "").toBe(drafts[id]);
  }

  async function switchPending() {
    const source = visible;
    const choices = raceIds.filter((id) => id !== visible && id !== router);
    visible = choices[random(choices.length)]!;
    const firstCall = switchTo.mock.calls.length;
    fireEvent.click(open[visible]);
    check();
    await nextTask();
    expect(switchTo.mock.calls.slice(firstCall)).not.toContainEqual([source]);
  }

  function typeDraft(step: number) {
    drafts[visible] = `pending draft ${seed} ${step} for ${visible}`;
    fireEvent.change(input(), { target: { value: drafts[visible] } });
  }

  async function submit(queue: boolean, step: number) {
    if (!drafts[visible]) typeDraft(step);
    const text = drafts[visible];
    const count = queue ? enqueue.mock.calls.length : send.mock.calls.length;
    act(() => useChatStore.setState({ sessionStatus: queue ? "running" : "idle" }));
    fireEvent.keyDown(input(), { key: "Enter" });
    if (queue) {
      expect(enqueue).toHaveBeenCalledTimes(count + 1);
      expect(enqueue.mock.calls.at(-1)?.[0]).toBe(text);
      expect(enqueue.mock.calls.at(-1)?.[3]).toBe(visible);
    } else {
      expect(send).toHaveBeenCalledTimes(count + 1);
      expect(send.mock.calls.at(-1)?.[0]).toBe(text);
      expect(send.mock.calls.at(-1)?.[3]).toMatchObject({ pinnedConversationId: visible });
    }
    drafts[visible] = "";
    act(() => useChatStore.setState({ sessionStatus: "idle" }));
  }

  function finishCommit() {
    if (visible === router) return;
    fireEvent.click(finish[visible]);
    router = visible;
  }

  try {
    const runAction = async (step: number) => {
      const action =
        fixedPendingSteps[step] ?? ["switch", "draft", "send", "queue", "finish"][random(5)]!;
      trace.push(`${step}:${action}:${router}->${visible}`);
      if (action === "switch") await switchPending();
      else if (action === "draft") typeDraft(step);
      else if (action === "send" || action === "queue") await submit(action === "queue", step);
      else finishCommit();
      check();
    };
    let sequence = Promise.resolve();
    for (let step = 0; step < 30; step++) sequence = sequence.then(() => runAction(step));
    await sequence;
  } catch (error) {
    throw new Error(`pending switch race seed ${seed}, ${trace.join(" ")}`, { cause: error });
  } finally {
    qc.clear();
  }
}

function raceEnvInteger(name: string, fallback: number, min: number): number {
  const raw = process.env[name];
  const value = raw === undefined ? fallback : Number(raw);
  if (!Number.isInteger(value) || value < min) {
    throw new Error(`${name} must be an integer >= ${min}, got ${JSON.stringify(raw)}`);
  }
  return value;
}

// xorshift never leaves state 0, so seeds start at 1.
const raceSeed = raceEnvInteger("WARM_SWITCH_RACE_SEED", 1, 1);
// At least 2 so both the browser and the pending-commit suites get a seed.
const raceSeedCount = raceEnvInteger("WARM_SWITCH_RACE_SEEDS", 20, 2);
const browserRaceSeedCount = Math.ceil(raceSeedCount / 5);

describe("seeded warm switch race", () => {
  it.each(Array.from({ length: browserRaceSeedCount }, (_, index) => raceSeed + index))(
    "keeps session state isolated for seed %i",
    async (seed) => runSwitchRace(seed),
    30000,
  );
  it.each(
    Array.from(
      { length: raceSeedCount - browserRaceSeedCount },
      (_, index) => raceSeed + browserRaceSeedCount + index,
    ),
  )("keeps pending commits isolated for seed %i", async (seed) => runPendingRace(seed), 30000);
});
