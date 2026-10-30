import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Link, Outlet, MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { RoutingProvider, basenamedRouting, reactRouterRouting } from "@/lib/routing";

vi.mock("@/lib/analytics", () => ({
  useOmnigentPageView: vi.fn(),
  useOmnigentAnalytics: () => ({ trackClick: vi.fn() }),
}));
vi.mock("@/shell/AppShell", () => ({
  AppShell: () => (
    <div>
      <span>app shell</span>
      <Outlet />
    </div>
  ),
}));
vi.mock("@/pages/ChatPage", () => ({
  ChatPage: () => (
    <div>
      chat page
      <input aria-label="Page draft" />
      <Link to="/c/other">Another chat</Link>
    </div>
  ),
}));
vi.mock("@/pages/NotFoundPage", () => ({ NotFoundPage: () => <div>not found</div> }));
vi.mock("@/pages/UsagePage", () => ({ UsagePage: () => <div>usage page</div> }));
vi.mock("@/pages/TaskDetailPage", () => ({ TaskDetailPage: () => <div>task detail page</div> }));
vi.mock("@/pages/SettingsPage", async () => {
  const { useLocation, useNavigationType } = await import("react-router-dom");
  return {
    SettingsPage: () => {
      const { pathname, search, hash } = useLocation();
      return (
        <div data-testid="settings-location" data-navigation-type={useNavigationType()}>
          {pathname + search + hash}
        </div>
      );
    },
  };
});
vi.mock("@/extensions/ExtensionPageHost", () => ({
  ExtensionPageHost: ({ resolved }: { resolved: { page: { title: string } } }) => (
    <h1>{resolved.page.title}</h1>
  ),
}));
vi.mock("@/extensions/ExtensionProvider", () => ({
  useExtensionsLoading: () => false,
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

const chunkError = vi.hoisted(
  () => new TypeError("Failed to fetch dynamically imported module: /assets/old.js"),
);
const pageFault = vi.hoisted(() => ({ error: undefined as Error | null | undefined }));
vi.mock("@/pages/TasksPage", () => ({
  TasksPage: () => {
    if (pageFault.error !== null) throw pageFault.error ?? chunkError;
    return <p>Tasks recovered</p>;
  },
}));
vi.mock("@/pages/SetupPage", () => ({
  SetupPage: () => {
    if (pageFault.error !== null) throw pageFault.error ?? chunkError;
    return <p>Setup recovered</p>;
  },
}));

// Assert the recovery boundary surrounds both the normal and first-run route trees.
describe("chunk load recovery", () => {
  const reload = vi.fn();
  function suppressExpectedError(event: ErrorEvent) {
    if (event.error === chunkError) event.preventDefault();
  }

  beforeEach(() => {
    sessionStorage.clear();
    reload.mockReset();
    vi.stubGlobal("location", { reload });
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
    vi.spyOn(console, "error").mockImplementation(() => {});
    window.addEventListener("error", suppressExpectedError);
  });

  afterEach(() => {
    cleanup();
    window.removeEventListener("error", suppressExpectedError);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it.each([
    { path: "/tasks", basename: undefined, needsSetup: false },
    { path: "/tasks/templates", basename: undefined, needsSetup: false },
    { path: "/mount/tasks/templates", basename: "/mount", needsSetup: false },
    { path: "/mount/tasks", basename: "/mount", needsSetup: false },
    { path: "/setup", basename: undefined, needsSetup: true },
  ])("recovers from a failed page at $path", async ({ path, basename, needsSetup }) => {
    render(
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, accounts_enabled: needsSetup, needs_setup: needsSetup }}
      >
        <MemoryRouter initialEntries={[path]}>
          <App basename={basename} />
        </MemoryRouter>
      </CapabilitiesProvider>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to load this page");
    expect(reload).toHaveBeenCalledOnce();
  });
});

describe("ordinary page render failures", () => {
  const reload = vi.fn();
  const renderError = new Error("Page render failed");
  const suppressExpectedError = (event: ErrorEvent) => {
    if (event.error === renderError) event.preventDefault();
  };

  beforeEach(() => {
    pageFault.error = renderError;
    reload.mockReset();
    vi.stubGlobal("location", { reload });
    vi.spyOn(console, "error").mockImplementation(() => {});
    window.addEventListener("error", suppressExpectedError);
  });

  afterEach(() => {
    cleanup();
    pageFault.error = undefined;
    window.removeEventListener("error", suppressExpectedError);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it.each([
    { path: "/tasks", basename: undefined, needsSetup: false },
    { path: "/mount/tasks", basename: "/mount", needsSetup: false },
    { path: "/setup", basename: undefined, needsSetup: true },
  ])("offers recovery at $path without reloading", async ({ path, basename, needsSetup }) => {
    render(
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, accounts_enabled: needsSetup, needs_setup: needsSetup }}
      >
        <MemoryRouter initialEntries={[path]}>
          <App basename={basename} />
        </MemoryRouter>
      </CapabilitiesProvider>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to display this page.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();

    pageFault.error = null;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByText(needsSetup ? "Setup recovered" : "Tasks recovered"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
  });

  it.each([
    { path: "/tasks", basename: undefined },
    { path: "/mount/tasks", basename: "/mount" },
  ])(
    "resets an ordinary page failure when the pathname changes at $path",
    async ({ path, basename }) => {
      function HostNavigation() {
        const navigate = useNavigate();
        return (
          <button type="button" onClick={() => navigate(`${basename ?? ""}/c/healthy`)}>
            Navigate
          </button>
        );
      }
      render(
        <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
          <MemoryRouter initialEntries={[path]}>
            <HostNavigation />
            <App basename={basename} />
          </MemoryRouter>
        </CapabilitiesProvider>,
      );
      expect(await screen.findByRole("alert")).toHaveTextContent("Unable to display this page.");

      fireEvent.click(screen.getByRole("button", { name: "Navigate" }));

      expect(await screen.findByText("chat page")).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(reload).not.toHaveBeenCalled();
    },
  );
});

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

function renderRoute(
  path: string,
  features: Record<string, boolean> = {},
  info: typeof FALLBACK_SERVER_INFO | "loading" = { ...FALLBACK_SERVER_INFO, features },
) {
  return render(
    <CapabilitiesProvider info={info}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </CapabilitiesProvider>,
  );
}

describe("Canvas route", () => {
  it("preserves healthy chat state across pathname changes", () => {
    renderRoute("/c/first");
    const draft = screen.getByRole("textbox", { name: "Page draft" });
    fireEvent.change(draft, { target: { value: "Unsent work" } });
    fireEvent.click(screen.getByRole("link", { name: "Another chat" }));
    expect(screen.getByRole("textbox", { name: "Page draft" })).toBe(draft);
    expect(draft).toHaveValue("Unsent work");
  });

  it("shows a spinner, not 'not found', while server info is still loading", () => {
    renderRoute("/canvas", {}, "loading");
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
    expect(screen.queryByText("not found")).toBeNull();
  });

  it("renders not found once server info says the canvas feature is off", async () => {
    renderRoute("/canvas");
    expect(await screen.findByText("not found")).toBeInTheDocument();
  });

  it("leaves the Canvas landing surface to the shell when the feature is on", () => {
    renderRoute("/canvas", { canvas: true });
    expect(screen.getByText("app shell")).toBeInTheDocument();
    expect(screen.queryByText("not found")).toBeNull();
  });

  it("opens a normal chat inside the Canvas route when enabled", async () => {
    renderRoute("/canvas/c/session-one?canvas=project", { canvas: true });
    expect(await screen.findByText("chat page")).toBeInTheDocument();
    expect(screen.getByText("app shell")).toBeInTheDocument();
  });

  it("gates Canvas conversation deep links with the same feature flag", async () => {
    renderRoute("/canvas/c/session-one?canvas=project");
    expect(await screen.findByText("not found")).toBeInTheDocument();
    expect(screen.queryByText("chat page")).toBeNull();
  });
});

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

describe("Automation detail release feature route", () => {
  it("does not register /tasks/:taskId while automations_v2 is off", async () => {
    renderRoute("/tasks/st_1");
    expect(await screen.findByText("not found")).toBeInTheDocument();
    expect(screen.queryByText("task detail page")).toBeNull();
  });

  it("registers /tasks/:taskId while automations_v2 is on", async () => {
    renderRoute("/tasks/st_1", { automations_v2: true });
    expect(await screen.findByText("task detail page")).toBeInTheDocument();
  });
});

describe("Settings routes", () => {
  describe.each(["", "/embed"])("redirects under '%s'", (basename) => {
    it.each([
      ["/settings", "/settings/general"],
      ["/members", "/settings/members"],
      ["/policies", "/settings/policies"],
    ])("preserves URL context through the routing adapter: %s", async (from, to) => {
      const useHostNavigate = vi.fn(reactRouterRouting.useNavigate);
      const routing = basenamedRouting(basename, {
        ...reactRouterRouting,
        useNavigate: useHostNavigate,
      });
      render(
        <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
          <MemoryRouter initialEntries={[`${basename}${from}?o=123&view=compact#details`]}>
            <RoutingProvider value={routing}>
              <App basename={basename} />
            </RoutingProvider>
          </MemoryRouter>
        </CapabilitiesProvider>,
      );

      const location = await screen.findByTestId("settings-location");
      expect(location.textContent).toBe(`${basename}${to}?o=123&view=compact#details`);
      expect(location).toHaveAttribute("data-navigation-type", "REPLACE");
      expect(useHostNavigate).toHaveBeenCalled();
    });
  });

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
