import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { AutomationsIntro } from "./AutomationsIntro";
import { useViewerId } from "@/hooks/useViewerId";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import {
  getOmnigentHostConfig,
  setEmbedRoot,
  setOmnigentHostConfig,
  type OmnigentHostConfig,
} from "@/lib/host";
import { basenamedRouting, RoutingProvider } from "@/lib/routing";

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: vi.fn() }));
vi.mock("@/lib/nativeBridge", () => ({ isIOSShell: () => false }));

const fullInfo: ServerInfo = {
  ...FALLBACK_SERVER_INFO,
  scheduled_task_session_modes: true,
  managed_sandboxes_enabled: true,
  features: { session_orchestration: true },
};

function intro(info: ServerInfo | "loading" = fullInfo, basename = "", onClose = () => {}) {
  return (
    <MemoryRouter>
      <RoutingProvider value={basenamedRouting(basename)}>
        <CapabilitiesProvider info={info}>
          <AutomationsIntro onClose={onClose} />
        </CapabilitiesProvider>
      </RoutingProvider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(useViewerId).mockReturnValue("alice");
  setOmnigentHostConfig({});
});

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  setOmnigentHostConfig({});
  vi.restoreAllMocks();
});

describe("Automations first-visit introduction", () => {
  it("waits for both capabilities and identity", () => {
    const view = render(intro("loading"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    vi.mocked(useViewerId).mockReturnValue(null);
    view.rerender(intro());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    vi.mocked(useViewerId).mockReturnValue("alice");
    view.rerender(intro());
    expect(screen.getByRole("dialog", { name: "Introducing Automations" })).toBeVisible();
  });

  it("advances through the supported features and remembers completion across remounts", () => {
    const view = render(intro());
    expect(screen.getByText("1 of 4 · Run on a schedule")).toBeVisible();
    for (const title of ["Watch for new work", "Keep context between runs", "Make it yours"]) {
      fireEvent.click(screen.getByRole("button", { name: "Next" }));
      expect(screen.getByRole("button", { name: new RegExp(title) })).toHaveAttribute(
        "aria-current",
        "step",
      );
    }
    expect(screen.getByText("4 of 4 · Make it yours")).toBeVisible();
    expect(localStorage.length).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    view.unmount();
    render(intro());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it.each(["Maybe later", "Close", "Escape"])("remembers dismissal with %s", async (action) => {
    const onClose = vi.fn();
    const view = render(intro(fullInfo, "", onClose));
    if (action === "Escape") fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    else fireEvent.click(screen.getByRole("button", { name: action }));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    view.unmount();
    render(intro());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps dismissal separate by viewer, backend, and mount path", () => {
    const view = render(intro());
    fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
    vi.mocked(useViewerId).mockReturnValue("bob");
    view.rerender(intro());
    expect(screen.getByRole("dialog")).toBeVisible();
    vi.mocked(useViewerId).mockReturnValue("alice");
    view.rerender(intro());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    setOmnigentHostConfig({ serverIdentity: "workspace-two" });
    view.rerender(intro());
    expect(screen.getByRole("dialog")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
    view.rerender(intro(fullInfo, "/another-mount"));
    expect(screen.getByRole("dialog")).toBeVisible();
  });

  it("does not reuse or persist dismissal for embedded backends without an identity", () => {
    const standalone = render(intro());
    fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
    const originKey = localStorage.key(0)!;
    standalone.unmount();

    const previousConfig = getOmnigentHostConfig();
    const embeddedConfig: OmnigentHostConfig = { fetcher: vi.fn() };
    const write = vi.spyOn(Storage.prototype, "setItem");
    try {
      setOmnigentHostConfig(embeddedConfig);
      const backendA = render(intro());
      expect(screen.getByRole("dialog")).toBeVisible();
      fireEvent(window, new StorageEvent("storage", { key: originKey, newValue: "seen" }));
      expect(screen.getByRole("dialog")).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      backendA.rerender(intro());
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

      embeddedConfig.fetcher = vi.fn();
      setOmnigentHostConfig(embeddedConfig);
      backendA.rerender(intro());
      expect(screen.getByRole("dialog")).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
      expect(write).not.toHaveBeenCalled();
      backendA.unmount();
      const remounted = render(intro());
      expect(screen.getByRole("dialog")).toBeVisible();
      remounted.unmount();
    } finally {
      // The host setter preserves an installed fetcher unless its owner removes it.
      delete embeddedConfig.fetcher;
      setOmnigentHostConfig(previousConfig);
    }
  });

  it("only describes capabilities the deployment supports", () => {
    const view = render(intro(FALLBACK_SERVER_INFO));
    expect(screen.getByText("1 of 2 · Run on a schedule")).toBeVisible();
    expect(screen.queryByText("Watch for new work")).not.toBeInTheDocument();
    expect(screen.queryByText("Keep context between runs")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Make it yours/ }));
    expect(screen.getByText(/Keep the server and computer running/)).toBeVisible();
    expect(screen.queryByText("Computer or sandbox")).not.toBeInTheDocument();
    view.rerender(intro({ ...fullInfo, features: { session_orchestration: false } }));
    expect(screen.getByText("Keep context between runs")).toBeVisible();
    expect(screen.queryByText("Watch for new work")).not.toBeInTheDocument();
    view.rerender(intro({ ...fullInfo, scheduled_task_session_modes: false }));
    expect(screen.queryByText("Keep context between runs")).not.toBeInTheDocument();
    expect(screen.queryByText("Watch for new work")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Make it yours/ }));
    expect(
      screen.getByText(/Choose your agent and a connected computer or available sandbox/),
    ).toBeVisible();
    expect(screen.getByText("Computer or sandbox")).toBeVisible();
  });

  it("renders inside the managed portal and uses the deployment's branding", () => {
    const root = document.createElement("div");
    document.body.appendChild(root);
    setEmbedRoot(root);
    const view = render(
      intro(
        {
          ...fullInfo,
          branding: {
            app_name: "Isaac",
            heading: null,
            logos: { main: null, loading: null, favicon: null },
            powered_by: false,
          },
        },
        "/embedded",
      ),
    );
    expect(within(root).getByRole("dialog")).toBeVisible();
    expect(within(root).getByText("Put recurring work on autopilot with Isaac.")).toBeVisible();
    view.unmount();
    root.remove();
  });

  it("can still be dismissed when browser storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    render(intro());
    fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes when another tab dismisses the same guide", () => {
    const view = render(intro());
    fireEvent.click(screen.getByRole("button", { name: "Maybe later" }));
    const key = localStorage.key(0)!;
    view.unmount();
    localStorage.clear();
    render(intro());
    fireEvent(window, new StorageEvent("storage", { key, newValue: "seen" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
