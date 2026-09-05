// Wiring tests: every page mounted OUTSIDE the AppShell (login, register,
// first-run setup, approve — plus the shared v2 AuthCardShell) must render
// the ElectronWindowDragStrip on the macOS Electron desktop shell. There the
// native title bar is hidden (titleBarStyle "hiddenInset"), so a screen
// without a `-webkit-app-region: drag` element leaves the desktop window
// impossible to move at all — the AppShell's own strip never mounts on these
// screens, so each must carry its own.

import { cleanup, render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovePage } from "./ApprovePage";
import { LoginPage } from "./LoginPage";
import { RegisterPage } from "./RegisterPage";
import { SetupPage } from "./SetupPage";
import { AuthCardShell } from "./onboarding/AuthCardShell";

vi.mock("@/lib/accountsApi", () => ({
  // Never resolves: keeps LoginPage's already-authed auto-bounce inert so the
  // form (and its strip) stays mounted for the assertion.
  getMe: vi.fn(() => new Promise(() => {})),
  login: vi.fn(),
  register: vi.fn(),
  setup: vi.fn(),
}));
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(() => new Promise(() => {})),
}));
// The animated WebGL panel can't initialize under jsdom; the shell's layout
// (which hosts the strip) is what's under test.
vi.mock("@/components/onboarding/AnimatedOmnigentPanel", () => ({
  AnimatedOmnigentPanel: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
}));

const MAC_ELECTRON_UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) omnigent-desktop/1.0.0 Chrome/126.0.0.0 " +
  "Electron/31.0.0 Safari/537.36";

beforeEach(() => {
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = { kind: "electron" };
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(MAC_ELECTRON_UA);
});

afterEach(() => {
  cleanup();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
  vi.restoreAllMocks();
  window.localStorage.clear();
});

function expectDragStrip(container: HTMLElement, screen: string) {
  expect(
    container.querySelector(".electron-standalone-drag-strip"),
    `${screen} must render a window-drag strip on the macOS Electron shell`,
  ).not.toBeNull();
}

describe("outside-AppShell screens keep the desktop window draggable", () => {
  it("login page renders the drag strip", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/login"]}>
        <LoginPage />
      </MemoryRouter>,
    );
    expectDragStrip(container, "/login");
  });

  it("register page renders the drag strip", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/register"]}>
        <RegisterPage />
      </MemoryRouter>,
    );
    expectDragStrip(container, "/register");
  });

  it("first-run setup page renders the drag strip", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/"]}>
        <SetupPage />
      </MemoryRouter>,
    );
    expectDragStrip(container, "first-run setup");
  });

  it("approve page renders the drag strip", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/approve/sess_1/eli_1"]}>
        <Routes>
          <Route path="/approve/:sessionId/:elicitationId" element={<ApprovePage />} />
        </Routes>
      </MemoryRouter>,
    );
    expectDragStrip(container, "/approve");
  });

  it("v2 auth card shell renders the drag strip", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/login"]}>
        <AuthCardShell>
          <div>card body</div>
        </AuthCardShell>
      </MemoryRouter>,
    );
    expectDragStrip(container, "v2 auth card shell");
  });
});
