// Tests for the title-bar server picker: it shows the current server's
// nickname (falling back to its host) and lists the other recent servers by
// nickname/host, switching to one on select. See TitleBarServerPicker.tsx.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ServerPickerInfo } from "@/lib/nativeBridge";

const mocks = vi.hoisted(() => ({
  getServerPicker: vi.fn(),
  switchServer: vi.fn(),
  openServerSetup: vi.fn(),
}));

vi.mock("@/lib/nativeBridge", () => ({
  getServerPicker: mocks.getServerPicker,
  switchServer: mocks.switchServer,
  openServerSetup: mocks.openServerSetup,
}));

import { TitleBarServerPicker } from "./TitleBarServerPicker";

const INFO: ServerPickerInfo = {
  currentOrigin: "https://prod.example.com",
  recentServers: [
    { url: "https://prod.example.com/ml/omnigent", label: "Prod (west)" },
    { url: "http://localhost:6767", label: "Local dev" },
    { url: "https://other.example.com", label: "" },
  ],
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("TitleBarServerPicker", () => {
  it("labels the trigger with the current server's nickname", async () => {
    mocks.getServerPicker.mockResolvedValue(INFO);

    render(<TitleBarServerPicker />);

    // Falls back through null until getServerPicker resolves.
    expect(await screen.findByText("Omnigent — Prod (west)")).toBeInTheDocument();
  });

  it("renders nothing outside the Electron shell (null picker)", async () => {
    mocks.getServerPicker.mockResolvedValue(null);

    const { container } = render(<TitleBarServerPicker />);

    // Give the resolved-null effect a tick; the component stays empty.
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
  });

  it("lists other servers by nickname/host and switches on select", async () => {
    mocks.getServerPicker.mockResolvedValue(INFO);

    render(<TitleBarServerPicker />);
    const trigger = await screen.findByText("Omnigent — Prod (west)");

    // Radix DropdownMenu opens on pointerdown, not click.
    fireEvent.pointerDown(trigger, { button: 0 });

    // The labelled other server shows its nickname; the unlabelled one, its host.
    expect(screen.getByText("Local dev")).toBeInTheDocument();
    expect(screen.getByText("other.example.com")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Local dev"));
    expect(mocks.switchServer).toHaveBeenCalledWith("http://localhost:6767");
  });
});
