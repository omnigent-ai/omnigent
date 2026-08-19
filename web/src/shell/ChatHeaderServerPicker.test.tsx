import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatHeaderServerPicker } from "./ChatHeaderServerPicker";

const getServerPicker = vi.fn();
const switchServer = vi.fn();
const openServerSetup = vi.fn();

vi.mock("@/lib/nativeBridge", () => ({
  getServerPicker: () => getServerPicker(),
  switchServer: (url: string) => switchServer(url),
  openServerSetup: () => openServerSetup(),
}));

async function openMenu() {
  const trigger = await screen.findByTestId("chat-header-server-picker");
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  return trigger;
}

beforeEach(() => {
  getServerPicker.mockReset();
  switchServer.mockReset();
  openServerSetup.mockReset();
});

afterEach(cleanup);

describe("ChatHeaderServerPicker", () => {
  it("renders nothing outside the Electron shell", async () => {
    getServerPicker.mockResolvedValue(null);
    const { container } = render(<ChatHeaderServerPicker />);

    await waitFor(() => expect(getServerPicker).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the current host and recent servers", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: ["http://localhost:8000/", "https://other.example.com/"],
    });
    render(<ChatHeaderServerPicker />);

    const trigger = await openMenu();
    expect(trigger).toHaveAttribute("aria-label", "Server: localhost:8000. Switch server");
    expect(screen.getAllByText("localhost:8000")).toHaveLength(2);
    expect(await screen.findByText("other.example.com")).toBeInTheDocument();
  });

  it("shows the product name when no conversation title is available", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: [],
    });
    render(<ChatHeaderServerPicker showBrand />);

    expect(await screen.findByText("Omnigent")).toBeInTheDocument();
  });

  it("switches servers and opens setup", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: ["https://other.example.com/"],
    });
    render(<ChatHeaderServerPicker />);

    await openMenu();
    fireEvent.click(await screen.findByText("other.example.com"));
    await waitFor(() => expect(switchServer).toHaveBeenCalledWith("https://other.example.com/"));

    await openMenu();
    fireEvent.click(await screen.findByText("Connect to new server…"));
    await waitFor(() => expect(openServerSetup).toHaveBeenCalled());
  });

  it("falls back to raw server strings that are not URLs", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "not-a-url",
      recentServers: ["also-not-a-url"],
    });
    render(<ChatHeaderServerPicker />);

    const trigger = await openMenu();
    expect(trigger).toHaveAttribute("aria-label", "Server: not-a-url. Switch server");
    expect(await screen.findByText("also-not-a-url")).toBeInTheDocument();
  });
});
