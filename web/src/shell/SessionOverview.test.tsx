import type { ComponentProps, ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { useChatStore } from "@/store/chatStore";
import type { ModelUsage } from "@/lib/types";
import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { SessionOverview } from "./SessionOverview";

const mobile = vi.hoisted(() => ({ value: false }));
const useChildSessionsMock = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => mobile.value }));
vi.mock("@/hooks/useChildSessions", () => ({
  useChildSessions: useChildSessionsMock,
}));

type Props = ComponentProps<typeof SessionOverview>;
let host: HTMLDivElement;
let props: Props;
let width: number;
const resizeCallbacks = new Set<() => void>();

function Wrapper({ children }: { children: ReactNode }) {
  return (
    <MemoryRouter>
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, features: { session_overview: true } }}
      >
        {children}
      </CapabilitiesProvider>
    </MemoryRouter>
  );
}

function file(path: string, added: number | null, removed: number | null): WorkspaceChangedFile {
  return {
    path,
    name: path,
    status: "modified",
    bytes: null,
    modified_at: null,
    lines_added: added,
    lines_removed: removed,
  };
}

beforeEach(() => {
  useChildSessionsMock.mockReset().mockReturnValue({
    children: [{ id: "child", task_summary: "Review changes", busy: true, own_cost_usd: 0.25 }],
    isLoading: false,
    error: null,
  });
  mobile.value = false;
  width = 1200;
  localStorage.clear();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      callback: () => void;
      constructor(callback: () => void) {
        this.callback = callback;
        resizeCallbacks.add(callback);
      }
      observe() {}
      unobserve() {}
      disconnect() {
        resizeCallbacks.delete(this.callback);
      }
    },
  );
  host = document.createElement("div");
  const dockTarget = host.appendChild(document.createElement("div"));
  const chatWidthMeasure = host.appendChild(document.createElement("div"));
  document.body.appendChild(host);
  vi.spyOn(host, "getBoundingClientRect").mockImplementation(() => new DOMRect(0, 0, width, 800));
  vi.spyOn(chatWidthMeasure, "getBoundingClientRect").mockImplementation(
    () => new DOMRect(0, 0, 768, 0),
  );
  props = {
    sessionId: "session",
    title: "Example session",
    dockTarget,
    chatWidthMeasure,
    dockAllowed: true,
    showChanges: true,
    onOpenChanges: vi.fn(),
    onOpenAgents: vi.fn(),
    changedFiles: {
      data: { available: true, data: [file("app.ts", 12, 4), file("asset.png", null, null)] },
      isError: false,
    } as Props["changedFiles"],
  };
  const usage: ModelUsage = {
    inputTokens: 900,
    outputTokens: 100,
    totalTokens: 1000,
    cacheReadInputTokens: 700,
    cacheCreationInputTokens: 0,
    totalCostUsd: 99,
  };
  useChatStore.setState({
    conversationId: "session",
    loadingConversation: false,
    loadingSessionUsage: false,
    sessionCostUsd: 1.25,
    sessionUsageByModel: { "model-a": usage, "model-b": usage },
  });
});

afterEach(() => {
  cleanup();
  host.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  resizeCallbacks.clear();
});

describe("Session overview", () => {
  it.each([
    { isLoading: true, error: null, message: "Loading subagent costs" },
    { isLoading: false, error: null, message: "No subagents yet" },
    { isLoading: false, error: new Error("offline"), message: "Couldn’t refresh subagents." },
  ])("shows the subagent state: $message", ({ isLoading, error, message }) => {
    useChildSessionsMock.mockReturnValue({ children: [], isLoading, error });
    render(<SessionOverview {...props} />, { wrapper: Wrapper });
    expect(
      isLoading ? screen.getByRole("status", { name: message }) : screen.getByText(message),
    ).toBeInTheDocument();
  });

  it("reveals every subagent cost and loads nested breakdowns only while expanded", () => {
    const children = Array.from({ length: 21 }, (_, index) => ({
      id: `child-${index + 1}`,
      task_summary: `Agent ${index + 1}`,
      own_cost_usd: index + 1,
      busy: false,
    }));
    useChildSessionsMock.mockImplementation((id: string) => ({
      children:
        id === "session"
          ? children
          : [{ id: "grandchild", task_summary: "Nested agent", own_cost_usd: 0.5, busy: false }],
      isLoading: false,
      error: null,
    }));
    useChatStore.setState({ sessionCostUsd: 231.5 });
    const view = render(<SessionOverview {...props} />, { wrapper: Wrapper });
    expect(screen.queryByText("Agent 21")).toBeNull();
    expect(useChildSessionsMock.mock.calls.map(([id]) => id)).not.toContain("child-1");
    fireEvent.click(screen.getByRole("button", { name: "Show all 21 subagents" }));
    expect(screen.getByText("$21.00")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show nested costs for Agent 1" }));
    expect(useChildSessionsMock).toHaveBeenCalledWith("child-1", 10_000);
    expect(screen.getByRole("link", { name: /Nested agent/ })).toHaveAttribute(
      "href",
      "/c/grandchild",
    );
    expect(screen.getByText("$0.50")).toBeInTheDocument();
    expect(
      within(screen.getByRole("link", { name: /^Agent 1(?!\d)/ })).getByText("$1.00"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Hide nested costs for Agent 1" }));
    useChildSessionsMock.mockClear();
    view.rerender(<SessionOverview {...props} />);
    expect(screen.queryByText("Nested agent")).toBeNull();
    expect(useChildSessionsMock.mock.calls.map(([id]) => id)).not.toContain("child-1");
  });

  it("shows authoritative spend, cumulative tokens and partial workspace counts with working links", () => {
    render(<SessionOverview {...props} />, { wrapper: Wrapper });
    const card = within(screen.getByRole("complementary", { name: "Session overview" }));
    expect(card.getByText("$1.25")).toBeInTheDocument();
    expect(card.getByText("2K")).toBeInTheDocument();
    expect(card.getByText("Includes subagents")).toBeInTheDocument();
    expect(card.getByText("$0.25")).toBeInTheDocument();
    expect(card.getByRole("link", { name: /Review changes/ })).toHaveAttribute("href", "/c/child");
    expect(card.getByLabelText("12 lines added, 4 lines removed")).toBeInTheDocument();
    expect(card.getByText("Line counts unavailable for 1 file.")).toBeInTheDocument();
    fireEvent.click(card.getByText("View all agents →"));
    fireEvent.click(card.getByText("View changes →"));
    expect(props.onOpenAgents).toHaveBeenCalledOnce();
    expect(props.onOpenChanges).toHaveBeenCalledOnce();
  });

  it("remembers explicit dismissal and restores from the cost button", () => {
    const view = render(<SessionOverview {...props} />, { wrapper: Wrapper });
    expect(useChildSessionsMock).toHaveBeenCalledWith("session", 10_000);
    fireEvent.click(screen.getByRole("button", { name: "Hide session overview" }));
    expect(screen.queryByRole("complementary")).toBeNull();
    expect(screen.getByRole("button", { name: /Session overview, cost/ })).toHaveFocus();
    useChildSessionsMock.mockClear();
    view.rerender(<SessionOverview {...props} />);
    expect(useChildSessionsMock).not.toHaveBeenCalled();
    view.unmount();
    render(<SessionOverview {...props} />, { wrapper: Wrapper });
    expect(screen.queryByRole("complementary")).toBeNull();
    expect(useChildSessionsMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Session overview, cost/ }));
    expect(screen.getByRole("complementary")).toBeInTheDocument();
    expect(useChildSessionsMock).toHaveBeenCalledWith("session", 10_000);
  });

  it("restores after Workspace closes or space returns without changing the preference", () => {
    const view = render(<SessionOverview {...props} />, { wrapper: Wrapper });
    view.rerender(<SessionOverview {...props} dockAllowed={false} />);
    expect(screen.queryByRole("complementary")).toBeNull();
    expect(screen.getByRole("button", { name: /cost \$1.25/ })).toBeInTheDocument();
    view.rerender(<SessionOverview {...props} />);
    expect(screen.getByRole("complementary")).toBeInTheDocument();
    act(() => {
      width = 900;
      resizeCallbacks.forEach((callback) => callback());
    });
    expect(screen.queryByRole("complementary")).toBeNull();
    act(() => {
      width = 1200;
      resizeCallbacks.forEach((callback) => callback());
    });
    expect(screen.getByRole("complementary")).toBeInTheDocument();
    expect(localStorage.getItem("omnigent:session-overview-hidden")).toBeNull();
  });

  it.each([false, true])(
    "opens an on-demand overview without closing Workspace (mobile=%s)",
    (isMobile) => {
      mobile.value = isMobile;
      const view = render(<SessionOverview {...props} dockAllowed={false} />, { wrapper: Wrapper });
      expect(screen.queryByText("Includes subagents")).toBeNull();
      expect(useChildSessionsMock).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: /Session overview, cost/ }));
      expect(screen.getByText("Includes subagents")).toBeInTheDocument();
      expect(useChildSessionsMock).toHaveBeenCalledWith("session", 10_000);
      expect(props.onOpenChanges).not.toHaveBeenCalled();
      fireEvent.click(screen.getByText("View changes →"));
      expect(props.onOpenChanges).toHaveBeenCalledOnce();
      expect(screen.queryByText("Includes subagents")).toBeNull();
      useChildSessionsMock.mockClear();
      view.rerender(<SessionOverview {...props} dockAllowed={false} />);
      expect(useChildSessionsMock).not.toHaveBeenCalled();
    },
  );

  it("does not leak the previous session's usage while switching, and distinguishes zero from unknown", () => {
    const view = render(<SessionOverview {...props} />, { wrapper: Wrapper });
    view.rerender(<SessionOverview {...props} sessionId="next" title="Next session" />);
    expect(screen.queryByText("$1.25")).toBeNull();
    expect(screen.queryByText("2K")).toBeNull();
    expect(screen.queryByText("Unavailable")).toBeNull();
    const card = within(screen.getByRole("complementary", { name: "Session overview" }));
    expect(card.getByRole("status", { name: "Loading session cost" })).toBeInTheDocument();
    expect(card.getByRole("status", { name: "Loading tokens" })).toBeInTheDocument();
    act(() =>
      useChatStore.setState({
        conversationId: "next",
        sessionCostUsd: 0,
        sessionUsageByModel: null,
      }),
    );
    expect(screen.getByRole("button", { name: /cost \$0.00/ })).toBeInTheDocument();
  });

  it("shows loading usage until the independent request finishes without priced data", () => {
    useChatStore.setState({
      sessionCostUsd: null,
      sessionUsageByModel: null,
      loadingSessionUsage: true,
    });
    render(<SessionOverview {...props} />, { wrapper: Wrapper });
    const card = within(screen.getByRole("complementary", { name: "Session overview" }));
    expect(
      screen.getByRole("button", { name: "Session overview, cost loading" }),
    ).toBeInTheDocument();
    expect(card.getAllByRole("status")).toHaveLength(2);
    expect(screen.queryByText("Unavailable")).toBeNull();

    act(() => useChatStore.setState({ loadingSessionUsage: false }));
    expect(card.queryByRole("status")).toBeNull();
    expect(card.getAllByText("Unavailable")).toHaveLength(2);
  });

  it("keeps known usage visible while refreshing", () => {
    useChatStore.setState({ loadingSessionUsage: true });
    render(<SessionOverview {...props} />, { wrapper: Wrapper });
    const card = within(screen.getByRole("complementary", { name: "Session overview" }));
    expect(card.getByText("$1.25")).toBeInTheDocument();
    expect(card.getByText("2K")).toBeInTheDocument();
    expect(card.queryByRole("status")).toBeNull();
  });

  it("shows failed changes as unavailable and omits the section without file access", () => {
    const view = render(
      <SessionOverview {...props} changedFiles={{ ...props.changedFiles, isError: true }} />,
      { wrapper: Wrapper },
    );
    expect(screen.getByText("Changes unavailable")).toBeInTheDocument();
    expect(screen.queryByText("2 files changed")).toBeNull();
    view.rerender(<SessionOverview {...props} workspaceUnavailable />);
    expect(screen.getByText("Changes unavailable")).toBeInTheDocument();
    view.rerender(<SessionOverview {...props} showChanges={false} />);
    expect(screen.queryByText("Workspace changes")).toBeNull();
  });
});
