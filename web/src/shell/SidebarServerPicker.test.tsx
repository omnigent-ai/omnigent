import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarServerPicker } from "./SidebarServerPicker";

// The picker reaches the Electron shell only through nativeBridge, so the
// bridge is the seam: mocking it covers "inside the shell" (info resolves) and
// "plain browser" (resolves null) without having to fake a preload object.
const getServerPicker = vi.fn();
const switchServer = vi.fn();
const openServerSetup = vi.fn();

vi.mock("@/lib/nativeBridge", () => ({
  getServerPicker: () => getServerPicker(),
  switchServer: (url: string) => switchServer(url),
  openServerSetup: () => openServerSetup(),
}));

function renderPicker() {
  return render(
    <TooltipProvider>
      <SidebarServerPicker />
    </TooltipProvider>,
  );
}

/**
 * Open the menu. Radix opens its dropdown on pointerdown (not click), which is
 * how the rest of the suite drives these triggers.
 */
async function openMenu() {
  const trigger = await screen.findByTestId("sidebar-server-picker");
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  return trigger;
}

beforeEach(() => {
  getServerPicker.mockReset();
  switchServer.mockReset();
  openServerSetup.mockReset();
});

afterEach(cleanup);

describe("SidebarServerPicker", () => {
  it("renders nothing in a plain browser (bridge resolves null)", async () => {
    getServerPicker.mockResolvedValue(null);
    const { container } = renderPicker();

    // Wait out the resolve so this can't pass merely by asserting too early.
    await waitFor(() => expect(getServerPicker).toHaveBeenCalled());
    expect(screen.queryByTestId("sidebar-server-picker")).toBeNull();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the current host on the row and lists recents in the menu", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: [
        "http://localhost:8000/",
        "https://omnigents-3272836215725701.aws.databricksapps.com/",
        "https://omnigents-9147263058412098.aws.databricksapps.com/",
      ],
    });
    renderPicker();

    const trigger = await openMenu();
    expect(trigger).toHaveAttribute("aria-label", "Server: localhost:8000. Switch server");

    expect(await screen.findByText("Recents")).toBeInTheDocument();
    // localhost:8000 shows twice by design — once as the row's own label, once
    // as the menu's leading checked entry — and NOT a third time from recents,
    // which collapses into that entry.
    expect(screen.getAllByText("localhost:8000")).toHaveLength(2);
    expect(
      screen.getByText("omnigents-3272836215725701.aws.databricksapps.com"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("omnigents-9147263058412098.aws.databricksapps.com"),
    ).toBeInTheDocument();
  });

  it("shows managed servers before recents and switches to one", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://personal.example.com",
      managedServers: ["https://managed.example.com/ml/omnigents"],
      recentServers: ["https://managed.example.com/old-mount", "https://recent.example.com/"],
    });
    renderPicker();

    await openMenu();
    expect(await screen.findByText("Provided by your organization")).toBeInTheDocument();
    expect(screen.getAllByText("managed.example.com")).toHaveLength(1);
    expect(screen.getByText("Recents")).toBeInTheDocument();
    expect(screen.getByText("recent.example.com")).toBeInTheDocument();

    fireEvent.click(screen.getByText("managed.example.com"));
    await waitFor(() =>
      expect(switchServer).toHaveBeenCalledWith("https://managed.example.com/ml/omnigents"),
    );
  });

  it("shows a managed current server only in the organization section", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "https://managed.example.com",
      managedServers: ["https://managed.example.com/ml/omnigents"],
      recentServers: [],
    });
    renderPicker();

    await openMenu();
    expect(await screen.findByText("Provided by your organization")).toBeInTheDocument();
    // Once on the sidebar row and once as the checked managed menu item.
    expect(screen.getAllByText("managed.example.com")).toHaveLength(2);
    expect(screen.queryByText("Recents")).toBeNull();
  });

  it("switches to a recent server on select", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: ["https://other.example.com/"],
    });
    renderPicker();

    await openMenu();
    fireEvent.click(await screen.findByText("other.example.com"));

    await waitFor(() => expect(switchServer).toHaveBeenCalledWith("https://other.example.com/"));
  });

  it("opens the shell's setup page from 'Connect to new server…'", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: [],
    });
    renderPicker();

    await openMenu();
    fireEvent.click(await screen.findByText("Connect to new server…"));

    await waitFor(() => expect(openServerSetup).toHaveBeenCalled());
  });

  it("still offers 'Connect to new server…' with no recents", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: [],
    });
    renderPicker();

    await openMenu();

    // Only the current server is listed — but the menu is never a dead end.
    expect(await screen.findByText("Connect to new server…")).toBeInTheDocument();
  });

  it("falls back to the raw string when an origin won't parse as a URL", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "not-a-url",
      recentServers: ["also-not-a-url"],
    });
    renderPicker();

    const trigger = await openMenu();
    expect(trigger).toHaveAttribute("aria-label", "Server: not-a-url. Switch server");

    // Unparseable recents survive as-is rather than being dropped, so a
    // hand-edited settings file stays switchable instead of invisible.
    expect(await screen.findByText("also-not-a-url")).toBeInTheDocument();
  });
});
