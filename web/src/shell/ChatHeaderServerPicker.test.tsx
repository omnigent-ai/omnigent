import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatHeaderServerPicker } from "./ChatHeaderServerPicker";

const getServerPicker = vi.fn();
const switchServer = vi.fn();
const openServerSetup = vi.fn();
const connectWebServer = vi.fn();

vi.mock("@/lib/serverPicker", () => ({
  getServerPicker: () => getServerPicker(),
  switchServer: (url: string, runtime: string) => switchServer(url, runtime),
  openServerSetup: () => openServerSetup(),
  connectWebServer: (url: string) => connectWebServer(url),
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
  connectWebServer.mockReset();
});

afterEach(cleanup);

describe("ChatHeaderServerPicker", () => {
  it("renders nothing when the runtime has no server picker", async () => {
    getServerPicker.mockResolvedValue(null);
    const { container } = render(<ChatHeaderServerPicker />);

    await waitFor(() => expect(getServerPicker).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the current host and recent servers", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: ["http://localhost:8000/", "https://other.example.com/"],
      runtime: "desktop",
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
      runtime: "web",
    });
    render(<ChatHeaderServerPicker showBrand />);

    expect(await screen.findByText("Omnigent")).toBeInTheDocument();
  });

  it("switches servers and opens desktop setup", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000",
      recentServers: ["https://other.example.com/"],
      runtime: "desktop",
    });
    render(<ChatHeaderServerPicker />);

    await openMenu();
    fireEvent.click(await screen.findByText("other.example.com"));
    await waitFor(() =>
      expect(switchServer).toHaveBeenCalledWith("https://other.example.com/", "desktop"),
    );

    await openMenu();
    fireEvent.click(await screen.findByText("Connect to new server…"));
    await waitFor(() => expect(openServerSetup).toHaveBeenCalled());
  });

  it("opens a URL dialog and connects in the web runtime", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:8000/",
      recentServers: [],
      runtime: "web",
    });
    render(<ChatHeaderServerPicker />);

    await openMenu();
    fireEvent.click(await screen.findByText("Connect to new server…"));
    fireEvent.change(await screen.findByLabelText("Server URL"), {
      target: { value: "localhost:6771" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(connectWebServer).toHaveBeenCalledWith("localhost:6771");
    expect(openServerSetup).not.toHaveBeenCalled();
  });

  it("falls back to raw desktop server strings that are not URLs", async () => {
    getServerPicker.mockResolvedValue({
      currentOrigin: "not-a-url",
      recentServers: ["also-not-a-url"],
      runtime: "desktop",
    });
    render(<ChatHeaderServerPicker />);

    const trigger = await openMenu();
    expect(trigger).toHaveAttribute("aria-label", "Server: not-a-url. Switch server");
    expect(await screen.findByText("also-not-a-url")).toBeInTheDocument();
  });
});
