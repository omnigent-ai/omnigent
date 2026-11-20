import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ServerInfo } from "@/lib/capabilities";
import { AppShellOverlays, type AppShellOverlaysProps } from "./AppShellOverlays";

// The flag only chooses the palette; each palette's behavior has its own suites.
vi.mock("./CommandPalette", () => ({
  CommandPalette: ({ sessionsOnly }: { sessionsOnly?: boolean }) => (
    <div data-testid="cmdk-palette" data-sessions-only={String(Boolean(sessionsOnly))} />
  ),
}));
vi.mock("./QuickPickCommandPalette", () => ({
  QuickPickCommandPalette: ({ sessionsOnly }: { sessionsOnly?: boolean }) => (
    <div data-testid="quick-pick-palette" data-sessions-only={String(Boolean(sessionsOnly))} />
  ),
}));
vi.mock("@/components/KeyboardShortcutsDialog", () => ({ KeyboardShortcutsDialog: () => null }));
vi.mock("@/components/onboarding/HostImportReview", () => ({ ImportReviewGate: () => null }));
vi.mock("./RecentSessionsSwitcher", () => ({ RecentSessionsSwitcher: () => null }));

afterEach(cleanup);

function renderOverlays(features: Record<string, boolean> | "loading", sessionSearch = false) {
  const serverInfo =
    features === "loading" ? ("loading" as const) : ({ features } as unknown as ServerInfo);
  const props: AppShellOverlaysProps = {
    isEmbedded: false,
    serverInfo,
    sessionSearch,
    commandPaletteOpen: true,
    setCommandPaletteOpen: vi.fn(),
    toggleLeftSidebar: vi.fn(),
    toggleRightPanel: vi.fn(),
    gridRoute: false,
    allConversations: undefined,
    serverConversationId: undefined,
    updateOverlayHeight: 0,
  };
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AppShellOverlays {...props} />
    </QueryClientProvider>,
  );
}

describe("AppShellOverlays palette flag", () => {
  const notOn = [
    ["off", { quick_pick_palette: false }],
    ["missing, as on an older server", {}],
    ["unknown while server info loads", "loading" as const],
  ] as const;
  it.each(
    notOn.flatMap(([state, features]) =>
      [false, true].map((sessionSearch) => [state, features, sessionSearch] as const),
    ),
  )(
    "keeps the current palette when the flag is %s (sessions only: %s)",
    (_state, features, sessionSearch) => {
      const { getByTestId, queryByTestId } = renderOverlays(features, sessionSearch);
      expect(getByTestId("cmdk-palette").dataset.sessionsOnly).toBe(String(sessionSearch));
      expect(queryByTestId("quick-pick-palette")).toBeNull();
    },
  );

  it.each([false, true])(
    "shows the QuickPick palette when the flag is on (sessions only: %s)",
    (sessionSearch) => {
      const { getByTestId, queryByTestId } = renderOverlays(
        { quick_pick_palette: true },
        sessionSearch,
      );
      expect(getByTestId("quick-pick-palette").dataset.sessionsOnly).toBe(String(sessionSearch));
      expect(queryByTestId("cmdk-palette")).toBeNull();
    },
  );
});
