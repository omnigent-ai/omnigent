import type * as AgentsModule from "@/hooks/useAgents";
import type * as ConversationsModule from "@/hooks/useConversations";
import type * as SessionModule from "@/hooks/useSession";
import type * as HostsModule from "@/hooks/useHosts";
import type * as PermissionsModule from "@/hooks/usePermissions";
import type * as UnseenConversationsModule from "@/hooks/useUnseenConversations";
import type * as NewChatDialogModule from "@/shell/NewChatDialog";
import { act, cleanup, render, screen } from "@testing-library/react";
import { StrictMode, type ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAgents, useSessionAgent } from "@/hooks/useAgents";
import { useSession } from "@/hooks/useSession";
import { useLocation, useNavigate } from "@/lib/routing";
import { SessionNavigationProvider } from "@/lib/sessionNavigation";
import { canvasSessionHref } from "@/canvas/canvasNavigation";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { ChatPage, ChatSession } from "./ChatPage";

vi.mock("@/hooks/useAgents", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentsModule>()),
  useAgents: vi.fn(() => ({ data: undefined, error: null, refetch: vi.fn() })),
  useSessionAgent: vi.fn(() => ({ data: undefined })),
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof ConversationsModule>()),
  useConversations: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionModule>()),
  useSession: vi.fn(() => ({ session: null, isLoading: false, error: null })),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof HostsModule>()),
  useHosts: () => ({ data: undefined }),
  useHostModelOptions: () => ({ data: undefined }),
}));
vi.mock("@/hooks/usePermissions", async (importOriginal) => ({
  ...(await importOriginal<typeof PermissionsModule>()),
  usePermissions: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useUnseenConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UnseenConversationsModule>()),
  useMarkConversationSeen: vi.fn(),
}));
vi.mock("@/shell/NewChatDialog", async (importOriginal) => ({
  ...(await importOriginal<typeof NewChatDialogModule>()),
  NewChatLandingScreen: () => <div>New chat landing</div>,
}));

const initialState = useChatStore.getState();
const switchTo = vi.fn<typeof initialState.switchTo>().mockResolvedValue(undefined);

function RouteControls() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location">{location.pathname + location.search}</output>
      <button type="button" onClick={() => navigate("/c/route-b")}>
        Next session
      </button>
    </>
  );
}

function CanvasChatHost() {
  const { search } = useLocation();
  const sessionId = new URLSearchParams(search).get("session");
  return (
    <SessionNavigationProvider resolveHref={(id) => canvasSessionHref(id, search)}>
      {sessionId ? <ChatSession conversationId={sessionId} /> : <div>Canvas board</div>}
    </SessionNavigationProvider>
  );
}

function ReviewChatHost() {
  const { search } = useLocation();
  const sessionId = new URLSearchParams(search).get("thread");
  const resolveHref = (id: string | null) => {
    const params = new URLSearchParams(search);
    params.delete("thread");
    if (id !== null) params.set("thread", id);
    return `/review?${params.toString()}`;
  };
  return (
    <SessionNavigationProvider resolveHref={resolveHref}>
      {sessionId ? <ChatSession conversationId={sessionId} /> : <div>Review overview</div>}
    </SessionNavigationProvider>
  );
}

function routed(ui: ReactElement, path = "/c/route-a") {
  return (
    <MemoryRouter initialEntries={[path]}>
      <RouteControls />
      <Routes>
        <Route path="/c/:conversationId" element={ui} />
        <Route path="/" element={ui} />
        <Route path="/canvas" element={ui} />
        <Route path="/review" element={ui} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useChatStore.setState({ ...initialState, switchTo, loadingConversation: true });
});

afterEach(() => {
  cleanup();
  useChatStore.setState(initialState, true);
  conversationRegistry.clear();
});

describe("ChatPage route adapter", () => {
  it("binds the session selected by the route", () => {
    render(routed(<ChatPage />));

    expect(switchTo).toHaveBeenLastCalledWith("route-a");
    expect(useSession).toHaveBeenLastCalledWith("route-a");
    expect(screen.getByText("Loading conversation…")).toBeInTheDocument();
  });

  it("follows route changes without keeping the previous session binding", () => {
    render(routed(<ChatPage />));
    act(() => screen.getByRole("button", { name: "Next session" }).click());

    expect(switchTo).toHaveBeenLastCalledWith("route-b");
    expect(useSession).toHaveBeenLastCalledWith("route-b");
  });

  it("clears the active session and renders landing on the root route", () => {
    useChatStore.setState({ conversationId: "previous-session" });
    render(routed(<ChatPage />, "/"));

    expect(switchTo).toHaveBeenLastCalledWith(null);
    expect(useSession).toHaveBeenLastCalledWith(null);
    expect(screen.getByText("New chat landing")).toBeInTheDocument();
    expect(useAgents).toHaveBeenLastCalledWith({ enabled: true });
  });

  it("redirects a stale temporary session to landing without binding it", () => {
    render(routed(<ChatPage />, "/c/temp:missing"));

    expect(switchTo).not.toHaveBeenCalledWith("temp:missing");
    expect(switchTo).toHaveBeenLastCalledWith(null);
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/$/);
    expect(screen.getByText("New chat landing")).toBeInTheDocument();
  });

  it("preserves the server-driven superseded-session redirect", () => {
    render(routed(<ChatPage />));
    act(() => useChatStore.setState({ redirectToConversationId: "replacement" }));

    expect(screen.getByTestId("location")).toHaveTextContent("/c/replacement");
    expect(switchTo).toHaveBeenLastCalledWith("replacement");
    expect(useChatStore.getState().redirectToConversationId).toBeNull();
  });
});

describe("ChatSession custom host navigation", () => {
  it("uses a non-Canvas host for server-driven session replacement", () => {
    render(routed(<ReviewChatHost />, "/review?workspace=team&thread=source"));
    act(() => useChatStore.setState({ redirectToConversationId: "replacement" }));

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/review?workspace=team&thread=replacement",
    );
    expect(switchTo).toHaveBeenLastCalledWith("replacement");
    expect(useChatStore.getState().redirectToConversationId).toBeNull();
  });

  it("lets a non-Canvas host choose the destination for a cleared stale selection", () => {
    render(routed(<ReviewChatHost />, "/review?workspace=team&thread=temp:missing"));

    expect(switchTo).not.toHaveBeenCalledWith("temp:missing");
    expect(screen.getByTestId("location")).toHaveTextContent("/review?workspace=team");
    expect(screen.getByText("Review overview")).toBeInTheDocument();
  });
});

describe("ChatSession Canvas navigation", () => {
  it("replaces a superseded selection inside the same Canvas board", () => {
    render(
      routed(
        <CanvasChatHost />,
        "/canvas?canvas=board&session=source&file=a&diff=1&comment=c1&view=terminal&o=123",
      ),
    );
    act(() => useChatStore.setState({ redirectToConversationId: "replacement" }));

    expect(screen.getByTestId("location")).toHaveTextContent(
      "/canvas?canvas=board&o=123&session=replacement&view=chat",
    );
    expect(switchTo).toHaveBeenLastCalledWith("replacement");
    expect(useChatStore.getState().redirectToConversationId).toBeNull();
  });

  it("clears a stale temporary selection without navigating to the landing page", () => {
    render(routed(<CanvasChatHost />, "/canvas?canvas=board&session=temp:missing&view=chat&o=123"));

    expect(switchTo).not.toHaveBeenCalledWith("temp:missing");
    expect(screen.getByTestId("location")).toHaveTextContent("/canvas?canvas=board&o=123");
    expect(screen.getByText("Canvas board")).toBeInTheDocument();
    expect(screen.queryByText("New chat landing")).not.toBeInTheDocument();
  });
});

describe("ChatSession explicit selection", () => {
  it("uses its prop rather than a conflicting route parameter", () => {
    render(routed(<ChatSession conversationId="selected-session" />));

    expect(switchTo).toHaveBeenLastCalledWith("selected-session");
    expect(switchTo).not.toHaveBeenCalledWith("route-a");
    expect(useSession).toHaveBeenLastCalledWith("selected-session");
    expect(useSessionAgent).toHaveBeenLastCalledWith("selected-session");
    expect(useAgents).toHaveBeenLastCalledWith({ enabled: false });
    expect(screen.getByTestId("location")).toHaveTextContent("/c/route-a");
  });

  it("binds a selected session on a route without conversation parameters", () => {
    render(routed(<ChatSession conversationId="selected-session" />, "/canvas"));

    expect(switchTo).toHaveBeenLastCalledWith("selected-session");
    expect(screen.getByText("Loading conversation…")).toBeInTheDocument();
    expect(screen.queryByText("New chat landing")).not.toBeInTheDocument();
  });

  it("follows prop changes and explicit clearing without navigating", () => {
    const { rerender } = render(routed(<ChatSession conversationId="session-a" />));
    rerender(routed(<ChatSession conversationId="session-b" />));

    expect(switchTo).toHaveBeenLastCalledWith("session-b");
    expect(useSession).toHaveBeenLastCalledWith("session-b");

    rerender(routed(<ChatSession conversationId={undefined} />));
    expect(switchTo).toHaveBeenLastCalledWith(null);
    expect(useSession).toHaveBeenLastCalledWith(null);
    expect(screen.getByText("New chat landing")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/c/route-a");
  });

  it("does not render an outgoing session's error under the new selection", () => {
    useChatStore.setState({
      conversationId: "session-a",
      loadingConversation: false,
      conversationLoadError: new Error("Session unavailable"),
    });
    const { rerender } = render(routed(<ChatSession conversationId="session-a" />));
    expect(screen.getByText("Conversation not found")).toBeInTheDocument();

    rerender(routed(<ChatSession conversationId="session-b" />));
    expect(screen.getByText("Loading conversation…")).toBeInTheDocument();
    expect(screen.queryByText("Conversation not found")).not.toBeInTheDocument();

    act(() => useChatStore.setState({ conversationId: "session-b" }));
    expect(screen.getByText("Conversation not found")).toBeInTheDocument();
    expect(screen.getByText("session-b")).toBeInTheDocument();
  });

  it("keeps temporary IDs out of server-scoped hooks", () => {
    useChatStore.setState({ conversationId: "temp:local" });
    render(routed(<ChatSession conversationId="temp:local" />));

    expect(switchTo).toHaveBeenLastCalledWith("temp:local");
    expect(useSession).toHaveBeenLastCalledWith(null);
    expect(useSessionAgent).toHaveBeenLastCalledWith(null);
    expect(screen.getByTestId("location")).toHaveTextContent("/c/route-a");
  });

  it("keeps the explicit selection under StrictMode effect replay", () => {
    render(<StrictMode>{routed(<ChatSession conversationId="selected-session" />)}</StrictMode>);

    expect(switchTo).toHaveBeenCalled();
    expect(switchTo.mock.calls.every(([id]) => id === "selected-session")).toBe(true);
    expect(useSession).toHaveBeenLastCalledWith("selected-session");
  });
});
