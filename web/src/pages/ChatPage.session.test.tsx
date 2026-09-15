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
      <output data-testid="location">{location.pathname}</output>
      <button type="button" onClick={() => navigate("/c/route-b")}>
        Next session
      </button>
    </>
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
