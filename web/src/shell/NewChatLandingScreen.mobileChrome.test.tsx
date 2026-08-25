// The landing screen's mobile chrome. Two rules, both about arriving at "/"
// on a phone:
//   1. The composer is not focused, so landing here — including on the way
//      back out of Settings — never throws up the keyboard unasked.
//   2. The iOS shell's native Chat/Terminal bar is pushed hidden: there is no
//      session here, so there is nothing to switch between.

import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as NativeBridgeModule from "@/lib/nativeBridge";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { isIOSShell, setNativeViewMode } from "@/lib/nativeBridge";
import { NewChatLandingScreen } from "./NewChatDialog";

vi.mock("@/lib/routing", () => ({
  useNavigate: () => vi.fn(),
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
}));

vi.mock("@/store/chatStore", () => ({ setPendingInitialPrompt: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(() => ({ data: [] })),
  useInstallHarness: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useInstallingHarnesses: vi.fn(() => new Set<string>()),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: () => ({ data: undefined }),
  useCreateHostDirectory: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/useHostWorktrees", () => ({
  useHostWorktrees: () => ({ data: [], isError: false }),
}));
vi.mock("@/hooks/useDirectorySessions", () => ({ useDirectorySessions: () => ({ data: [] }) }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: () => new Map<string, boolean>(),
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useProjects: () => ({ data: [], isLoading: false }),
  useProjectConfig: () => ({ data: undefined, isLoading: false }),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({}),
  useHarnessSetupSteps: () => ({}),
}));
// Only the two native entry points under test are stubbed; the rest of the
// bridge keeps its real (no-op outside a shell) behaviour.
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridgeModule>()),
  isIOSShell: vi.fn(() => false),
  setNativeViewMode: vi.fn(),
}));

/** Point `matchMedia` at a phone-width (or desktop-width) viewport. */
function setViewport(mobile: boolean): void {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: mobile && query.includes("max-width"),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

function renderLanding(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<NewChatLandingScreen />, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}

beforeEach(() => {
  vi.mocked(useHosts).mockReturnValue({
    data: [{ host_id: "host_1", name: "laptop", owner: "corey", status: "online" } as Host],
  } as ReturnType<typeof useHosts>);
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [
      {
        id: "ag_hello",
        name: "hello_world",
        display_name: "Hello World",
        description: null,
        harness: null,
        skills: [],
      } as AvailableAgent,
    ],
  } as ReturnType<typeof useAvailableAgents>);
  vi.mocked(isIOSShell).mockReturnValue(false);
  vi.mocked(setNativeViewMode).mockClear();
  setViewport(false);
});

afterEach(() => {
  cleanup();
  setViewport(false);
});

describe("NewChatLandingScreen keyboard behaviour", () => {
  it("focuses the composer on desktop so typing can start immediately", () => {
    renderLanding();

    expect(document.activeElement).toBe(screen.getByTestId("new-chat-landing-input"));
  });

  it("leaves the composer unfocused on a phone, so arriving never raises the keyboard", () => {
    setViewport(true);
    renderLanding();

    expect(document.activeElement).not.toBe(screen.getByTestId("new-chat-landing-input"));
  });
});

describe("NewChatLandingScreen native Chat/Terminal bar", () => {
  it("pushes the bar hidden in the iOS shell — there is no session to switch", () => {
    vi.mocked(isIOSShell).mockReturnValue(true);
    renderLanding();

    expect(vi.mocked(setNativeViewMode)).toHaveBeenCalledWith({
      mode: "chat",
      terminalEnabled: false,
      terminalStartingUp: false,
      visible: false,
    });
  });

  it("leaves the native bridge alone outside the iOS shell", () => {
    renderLanding();

    expect(vi.mocked(setNativeViewMode)).not.toHaveBeenCalled();
  });
});
