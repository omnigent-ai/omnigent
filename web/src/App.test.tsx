import { render, screen } from "@testing-library/react";
import { Outlet, MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";

vi.mock("@/lib/analytics", () => ({ useOmnigentPageView: vi.fn() }));
vi.mock("@/shell/AppShell", () => ({
  AppShell: () => (
    <div>
      <span>app shell</span>
      <Outlet />
    </div>
  ),
}));
vi.mock("@/pages/ChatPage", () => ({ ChatPage: () => <div>chat page</div> }));
vi.mock("@/pages/NotFoundPage", () => ({ NotFoundPage: () => <div>not found</div> }));
vi.mock("@/pages/UsagePage", () => ({ UsagePage: () => <div>usage page</div> }));
vi.mock("@/pages/SettingsPage", async () => {
  const { useLocation } = await import("react-router-dom");
  return {
    SettingsPage: () => <div data-testid="settings-location">{useLocation().pathname}</div>,
  };
});
vi.mock("@/extensions/ExtensionPageHost", () => ({
  ExtensionPageHost: ({ resolved }: { resolved: { page: { title: string } } }) => (
    <h1>{resolved.page.title}</h1>
  ),
}));
vi.mock("@/extensions/ExtensionProvider", () => ({
  useExtensions: () => [
    {
      object: "extension",
      id: "acme.review",
      display_name: "Acme Review",
      distribution: "acme-review",
      version: "1.0.0",
      extension_api: 1,
      status: "enabled",
      permissions: [],
      pages: [
        {
          id: "acme.review.dashboard",
          title: "Review dashboard",
          route: "dashboard",
          view: "review-dashboard",
        },
      ],
      primary_navigation: [],
      browser: {
        declared: true,
        has_styles: false,
        digest: "digest",
        script_url: "/script",
        style_url: null,
      },
    },
  ],
}));

import App from "./App";

function renderUsageRoute(enabled: boolean) {
  const info: typeof FALLBACK_SERVER_INFO = {
    ...FALLBACK_SERVER_INFO,
    features: enabled ? { usage_page: true } : {},
  };
  return render(
    <CapabilitiesProvider info={info}>
      <MemoryRouter initialEntries={["/usage"]}>
        <App />
      </MemoryRouter>
    </CapabilitiesProvider>,
  );
}

function renderRoute(path: string) {
  return render(
    <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </CapabilitiesProvider>,
  );
}

describe("Extension page routes", () => {
  it("renders a catalog-owned namespaced page", async () => {
    render(
      <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
        <MemoryRouter initialEntries={["/extensions/acme.review/dashboard"]}>
          <App />
        </MemoryRouter>
      </CapabilitiesProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Review dashboard" })).toBeInTheDocument();
  });

  it("matches extension pages under an embedded basename", async () => {
    render(
      <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
        <MemoryRouter initialEntries={["/mount/extensions/acme.review/dashboard"]}>
          <App basename="/mount" />
        </MemoryRouter>
      </CapabilitiesProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Review dashboard" })).toBeInTheDocument();
  });

  it("rejects unknown extension pages", async () => {
    render(
      <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
        <MemoryRouter initialEntries={["/extensions/acme.review/missing"]}>
          <App />
        </MemoryRouter>
      </CapabilitiesProvider>,
    );

    expect(await screen.findByText("not found")).toBeInTheDocument();
  });
});

describe("Usage release feature route", () => {
  it("does not register /usage while the feature is off", async () => {
    renderUsageRoute(false);
    expect(await screen.findByText("not found")).toBeInTheDocument();
    expect(screen.queryByText("usage page")).toBeNull();
  });

  it("registers /usage while the feature is on", async () => {
    renderUsageRoute(true);
    expect(await screen.findByText("usage page")).toBeInTheDocument();
    expect(screen.queryByText("not found")).toBeNull();
  });
});

describe("Settings routes", () => {
  it("redirects bare settings to the canonical General section", async () => {
    renderRoute("/settings");

    expect(await screen.findByTestId("settings-location")).toHaveTextContent("/settings/general");
  });

  it("preserves an explicit settings section", async () => {
    renderRoute("/settings/appearance");

    expect(await screen.findByTestId("settings-location")).toHaveTextContent(
      "/settings/appearance",
    );
  });
});
