import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ServerInfo } from "@/lib/capabilities";
import type { AppShellOverlaysProps } from "./AppShellOverlays";

// The flag only chooses the palette; each palette's behavior has its own suites.
vi.mock("./CommandPalette", () => ({
  CommandPalette: ({ sessionsOnly }: { sessionsOnly?: boolean }) => (
    <div data-testid="cmdk-palette" data-sessions-only={String(Boolean(sessionsOnly))} />
  ),
}));
vi.mock("@/components/KeyboardShortcutsDialog", () => ({ KeyboardShortcutsDialog: () => null }));
vi.mock("@/components/onboarding/HostImportReview", () => ({ ImportReviewGate: () => null }));
vi.mock("./RecentSessionsSwitcher", () => ({ RecentSessionsSwitcher: () => null }));

afterEach(cleanup);
// Which flagged modules this case imported. Each case loads the shell and these mocks afresh,
// so a flag-on case can't hide or cause a load in a later one.
const loaded = new Set<string>();
beforeEach(() => {
  vi.resetModules();
  loaded.clear();
  vi.doMock("./QuickPickCommandPalette", () => {
    loaded.add("QuickPickCommandPalette");
    return {
      QuickPickCommandPalette: ({ sessionsOnly }: { sessionsOnly?: boolean }) => (
        <div data-testid="quick-pick-palette" data-sessions-only={String(Boolean(sessionsOnly))} />
      ),
    };
  });
  vi.doMock("./ThemePicker", () => {
    loaded.add("ThemePicker");
    return { ThemePicker: () => <div data-testid="theme-picker" /> };
  });
});

async function renderOverlays(
  features: Record<string, boolean> | "loading",
  sessionSearch = false,
) {
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
    goToSession: vi.fn(),
    gridRoute: false,
    allConversations: undefined,
    serverConversationId: undefined,
    updateOverlayHeight: 0,
  };
  const { AppShellOverlays } = await import("./AppShellOverlays");
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
    async (_state, features, sessionSearch) => {
      const { getByTestId, queryByTestId } = await renderOverlays(features, sessionSearch);
      expect(getByTestId("cmdk-palette").dataset.sessionsOnly).toBe(String(sessionSearch));
      expect(queryByTestId("quick-pick-palette")).toBeNull();
      expect(queryByTestId("theme-picker")).toBeNull();
      // Without the flag, neither the QuickPick palette nor its theme picker is even fetched.
      expect([...loaded]).toEqual([]);
    },
  );

  it.each([false, true])(
    "shows the QuickPick palette when the flag is on (sessions only: %s)",
    async (sessionSearch) => {
      const { findByTestId, queryByTestId } = await renderOverlays(
        { quick_pick_palette: true },
        sessionSearch,
      );
      expect((await findByTestId("quick-pick-palette")).dataset.sessionsOnly).toBe(
        String(sessionSearch),
      );
      expect(await findByTestId("theme-picker")).toBeTruthy();
      expect(queryByTestId("cmdk-palette")).toBeNull();
      expect([...loaded].sort()).toEqual(["QuickPickCommandPalette", "ThemePicker"]);
    },
  );
});
