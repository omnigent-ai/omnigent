// Warm session switches with `warm_switch_keep_mounted_ui`: a sidebar-style
// click switches the store with the navigation, so the commit landing
// `/c/:id` skips the hydrating placeholder and the chat surface stays mounted.
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
  features: Record<string, boolean>,
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
  const info = { ...FALLBACK_SERVER_INFO, ...options.info, features };
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
  renderPage({ warm_switch_keep_mounted_ui: true }, qc, {
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
  enabled: boolean,
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
  renderPage({ warm_switch_keep_mounted_ui: enabled }, qc, {
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

describe("ChatPage warm switch (warm_switch_keep_mounted_ui)", () => {
  it("flag on, live target: the surface stays mounted and no placeholder renders", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    seedLiveEntry("conv_b", "beta transcript");
    renderPage({ warm_switch_keep_mounted_ui: true }, qc);
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
      renderHeaderPage(true, child, frames);
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
      renderHeaderPage(true, false, frames);
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

  it("leaves the header URL-owned with the flag off", async () => {
    const frames: ReturnType<typeof headerSnapshot>[] = [];
    renderHeaderPage(false, false, frames);
    await screen.findByRole("button", { name: "Project: Alpha project" });

    fireEvent.click(screen.getByRole("link", { name: "open beta" }));

    expect(headerSnapshot()).toMatchObject({
      title: "Alpha session",
      project: "Project: Alpha project",
      menuTarget: "conv_a",
      activeConversationId: "conv_a",
      urlConvId: "conv_a",
    });
    fireEvent.click(screen.getByRole("link", { name: "finish beta navigation" }));
    expect(headerSnapshot()).toMatchObject({
      title: "Beta session",
      menuTarget: "conv_b",
      activeConversationId: "conv_b",
      urlConvId: "conv_b",
    });
  });

  it("keeps header navigation URL-owned on Back and Forward", async () => {
    const frames: ReturnType<typeof headerSnapshot>[] = [];
    renderHeaderPage(true, false, frames, false);
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

  it("flag off: the placeholder commits and the surface remounts as before", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(qc);
    seedLiveEntry("conv_a", "alpha transcript");
    seedLiveEntry("conv_b", "beta transcript");
    renderPage({}, qc);
    await waitFor(() => expect(document.body.textContent).toContain("alpha transcript"));
    const surfaceBefore = transcriptNode();
    commitLog.length = 0;

    act(() => fireEvent.click(screen.getByRole("link", { name: "open beta" })));

    expect(useChatStore.getState().conversationId).toBe("conv_b");
    // The mismatch commit rendered the placeholder and tore the surface down.
    expect(commitLog).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          urlConvId: "conv_b",
          activeConversationId: "conv_a",
          loadingConversation: false,
        }),
      ]),
    );
    expect(transcriptNode()).not.toBe(surfaceBefore);
    await waitFor(() => expect(document.body.textContent).toContain("beta transcript"));
  });

  it("cold target still shows the placeholder with the flag on", async () => {
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
    renderPage({ warm_switch_keep_mounted_ui: true }, qc);
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
    renderPage({ warm_switch_keep_mounted_ui: true }, qc, { cancelBetaNavigation: true });
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
    renderPage({ warm_switch_keep_mounted_ui: true }, qc, { deferNavigation: true });
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
    renderPage({ warm_switch_keep_mounted_ui: true }, qc, { deferNavigation: true });
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
    renderPage({ warm_switch_keep_mounted_ui: true }, qc);
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
      renderPage({ warm_switch_keep_mounted_ui: true }, qc);
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
    renderPage({ warm_switch_keep_mounted_ui: true }, qc, {
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
    renderPage({ warm_switch_keep_mounted_ui: true }, qc, { initialConversationId: temporaryId });
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
