import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ServerSelectorV2, type ServerSelectorV2Setup } from "./ServerSelectorV2";

afterEach(cleanup);

function makeSetup(over: Partial<ServerSelectorV2Setup> = {}): ServerSelectorV2Setup {
  return {
    initialUrl: "http://localhost:6767",
    recentServers: [],
    managedServers: [],
    onConnect: vi.fn().mockResolvedValue({}),
    onStartLocal: vi.fn().mockResolvedValue({ ok: true }),
    onCopy: vi.fn(),
    onCheckServer: vi.fn().mockResolvedValue({ status: "ok" }),
    onCloudSetup: vi.fn(),
    onSwitchToLegacy: vi.fn(),
    ...over,
  };
}

describe("ServerSelectorV2", () => {
  it("starts on the landing step", () => {
    render(<ServerSelectorV2 setup={makeSetup()} />);
    expect(screen.getByRole("heading", { name: "Meet Omnigent" })).toBeInTheDocument();
  });

  it("Get started locally shows the local intro (not install yet)", () => {
    render(<ServerSelectorV2 setup={makeSetup()} />);
    fireEvent.click(screen.getByRole("button", { name: /get started locally/i }));
    // Local/Cloud switcher was replaced by a single local intro; install starts
    // only after clicking Install Omnigent.
    expect(screen.getByRole("heading", { name: /set up omnigent locally/i })).toBeInTheDocument();
    expect(screen.queryByText(/starting the local server/i)).not.toBeInTheDocument();
  });

  it("Install Omnigent from the local intro starts the install", () => {
    render(<ServerSelectorV2 setup={makeSetup()} />);
    fireEvent.click(screen.getByRole("button", { name: /get started locally/i }));
    fireEvent.click(screen.getByRole("button", { name: /install omnigent/i }));
    expect(screen.getByText(/starting the local server/i)).toBeInTheDocument();
  });

  it("a returning user (has recents) starts on the server list, not the landing", () => {
    render(
      <ServerSelectorV2 setup={makeSetup({ recentServers: ["https://team.example.com/"] })} />,
    );
    expect(screen.getByText(/^Recents$/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Meet Omnigent" })).not.toBeInTheDocument();
  });

  it("an installed CLI alone doesn't make a returning user", () => {
    render(<ServerSelectorV2 setup={makeSetup({ installed: true })} />);
    expect(screen.getByRole("heading", { name: "Meet Omnigent" })).toBeInTheDocument();
  });

  it("a returning MDM user (no recents — presets are excluded) starts on the preset list", () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({
          connectedBefore: true,
          managedServers: ["https://field-eng-omni.aws.databricksapps.com"],
        })}
      />,
    );
    expect(screen.getByText(/preset \(by your organization\)/i)).toBeInTheDocument();
  });

  it("a returning user who cleared every server starts on the landing", () => {
    render(<ServerSelectorV2 setup={makeSetup({ connectedBefore: true })} />);
    expect(screen.getByRole("heading", { name: "Meet Omnigent" })).toBeInTheDocument();
  });

  it("Join your team advances to the server-select step", () => {
    render(<ServerSelectorV2 setup={makeSetup()} />);
    fireEvent.click(screen.getByRole("button", { name: /join your team/i }));
    expect(screen.getByLabelText("Server URL")).toBeInTheDocument();
  });

  it("a local install that's down reads 'Start Omnigent' and boots it", async () => {
    const onConnect = vi.fn().mockResolvedValue({});
    const onStartLocal = vi.fn().mockResolvedValue({ ok: true });
    render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: true,
          onStartLocal,
          // Both loopback spellings of the local install get probed.
          recentServers: ["http://localhost:6767/", "http://127.0.0.1:6767/"],
          onConnect,
          onCheckServer: vi.fn().mockResolvedValue({ status: "unreachable" }),
        })}
      />,
    );
    expect(await screen.findAllByText("Not running")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Start Omnigent" }));
    expect(screen.getByText(/starting the local server/i)).toBeInTheDocument();
    await waitFor(() => expect(onStartLocal).toHaveBeenCalledOnce());
    expect(onConnect).not.toHaveBeenCalled();
  });

  it("a local install that's up reads 'Open Omnigent' and opens the exact URL", async () => {
    const onConnect = vi.fn().mockResolvedValue({});
    const onStartLocal = vi.fn().mockResolvedValue({ ok: true });
    render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: true,
          recentServers: ["http://localhost:6767/"],
          onConnect,
          onStartLocal,
        })}
      />,
    );
    expect(await screen.findByText("Omnigent server")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Omnigent" }));
    await waitFor(() => expect(onConnect).toHaveBeenCalledWith("http://localhost:6767/"));
    expect(onStartLocal).not.toHaveBeenCalled();
  });

  it("re-checks on click: a local install that stopped since the list loaded is booted", async () => {
    const onConnect = vi.fn().mockResolvedValue({});
    const onStartLocal = vi.fn().mockResolvedValue({ ok: true });
    const onCheckServer = vi
      .fn()
      .mockResolvedValueOnce({ status: "ok" })
      .mockResolvedValue({ status: "unreachable" });
    render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: true,
          recentServers: ["http://localhost:6767/"],
          onConnect,
          onStartLocal,
          onCheckServer,
        })}
      />,
    );
    expect(await screen.findByText("Omnigent server")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Omnigent" }));
    await waitFor(() => expect(onStartLocal).toHaveBeenCalledOnce());
    expect(onConnect).not.toHaveBeenCalled();
  });

  it.each(["http://localhost:8000/", "https://localhost:6767/team"])(
    "any other loopback URL (%s) connects to that exact URL, even when down",
    async (url) => {
      const onConnect = vi.fn().mockResolvedValue({});
      const onStartLocal = vi.fn().mockResolvedValue({ ok: true });
      render(
        <ServerSelectorV2
          setup={makeSetup({
            installed: true,
            recentServers: [url],
            onConnect,
            onStartLocal,
            onCheckServer: vi.fn().mockResolvedValue({ status: "unreachable" }),
          })}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "Open Omnigent" }));
      await waitFor(() => expect(onConnect).toHaveBeenCalledWith(url));
      expect(onStartLocal).not.toHaveBeenCalled();
    },
  );

  it("the local intro reads 'Start' when stopped and 'Open' when running", () => {
    const { unmount } = render(<ServerSelectorV2 setup={makeSetup({ installed: true })} />);
    fireEvent.click(screen.getByRole("button", { name: /get started locally/i }));
    expect(screen.getByRole("button", { name: "Start Omnigent" })).toBeInTheDocument();
    unmount();
    render(<ServerSelectorV2 setup={makeSetup({ installed: true, localServerRunning: true })} />);
    fireEvent.click(screen.getByRole("button", { name: /get started locally/i }));
    fireEvent.click(screen.getByRole("button", { name: "Open Omnigent" }));
    expect(screen.getByText(/connecting to the local server/i)).toBeInTheDocument();
  });

  it("Back from a failed start opened via 'Add server…' returns to the list, not the URL input", async () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: true,
          managedServers: ["https://field-eng-omni.aws.databricksapps.com"],
          onCheckServer: vi.fn().mockResolvedValue({ status: "unreachable" }),
          onStartLocal: vi.fn().mockResolvedValue({ ok: false, error: "boom" }),
        })}
      />,
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: /choose team url/i }), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: /add server/i }));
    fireEvent.change(screen.getByLabelText("Server URL"), { target: { value: "localhost:6767" } });
    fireEvent.click(screen.getByRole("button", { name: "Join" }));
    fireEvent.click(await screen.findByRole("button", { name: "Start Omnigent" }));
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(screen.getByText(/preset \(by your organization\)/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Server URL")).not.toBeInTheDocument();
  });

  it("'Add server…' in the preset dropdown opens the URL input; Back returns to the landing", () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({ managedServers: ["https://field-eng-omni.aws.databricksapps.com"] })}
      />,
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: /choose team url/i }), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: /add server/i }));
    expect(screen.getByLabelText("Server URL")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByRole("heading", { name: "Meet Omnigent" })).toBeInTheDocument();
  });

  it("picking a preset server from the landing shows its detail step", () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({ managedServers: ["https://field-eng-omni.aws.databricksapps.com"] })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(field-eng-omni\)/i }));
    expect(screen.getByRole("heading", { name: /you.?re in/i })).toBeInTheDocument();
    // Single server, no radio list to select from.
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
  });

  it("'Show all servers' from the preset detail reveals the full list (presets + recents)", () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({
          managedServers: ["https://field-eng-omni.aws.databricksapps.com"],
          recentServers: ["https://team.example.com/"],
        })}
      />,
    );
    // Returning (has recents) → opens on the list; Back reaches the landing.
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: /join your team \(field-eng-omni\)/i }));
    fireEvent.click(screen.getByRole("button", { name: /show all servers/i }));
    // Now on the full list: both sections present, so recents are reachable.
    expect(screen.getByText(/^Recents$/)).toBeInTheDocument();
    expect(screen.getByText(/preset \(by your organization\)/i)).toBeInTheDocument();
    expect(screen.getByText("team.example.com")).toBeInTheDocument();
  });

  it("opens directly on the server step when a connect error is present", () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({
          error: "Could not load http://dead/",
          recentServers: ["https://team.example.com/"],
        })}
      />,
    );
    // The error banner is only reachable on the server step — so being able to
    // see it proves the flow opened there rather than on the landing hero.
    expect(screen.getByText(/^Recents$/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load http://dead/");
  });

  it("the cog menu switches back to the legacy selector", () => {
    const onSwitchToLegacy = vi.fn();
    render(<ServerSelectorV2 setup={makeSetup({ onSwitchToLegacy })} />);
    // radix dropdown opens on pointerDown.
    fireEvent.pointerDown(screen.getByRole("button", { name: /server selector settings/i }), {
      button: 0,
    });
    fireEvent.click(
      screen.getByRole("menuitem", { name: /switch to legacy selector experience/i }),
    );
    expect(onSwitchToLegacy).toHaveBeenCalledOnce();
  });

  it("disables 'Switch to legacy' when the selector is env-forced", () => {
    const onSwitchToLegacy = vi.fn();
    render(
      <ServerSelectorV2 setup={makeSetup({ onSwitchToLegacy, switchToLegacyDisabled: true })} />,
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: /server selector settings/i }), {
      button: 0,
    });
    const item = screen.getByRole("menuitem", { name: /switch to legacy selector experience/i });
    expect(item).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(item);
    expect(onSwitchToLegacy).not.toHaveBeenCalled();
  });

  it("sets a real color scheme from the Appearance radios", () => {
    const onSetColorScheme = vi.fn();
    render(<ServerSelectorV2 setup={makeSetup({ onSetColorScheme })} />);
    fireEvent.pointerDown(screen.getByRole("button", { name: /server selector settings/i }), {
      button: 0,
    });
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Dark" }));
    expect(onSetColorScheme).toHaveBeenCalledWith("dark");
  });

  it("seeds the Appearance radio from the shell's current scheme", () => {
    // Returning to setup after the app set Dark: the radio reflects Dark, not
    // the "system" default.
    render(
      <ServerSelectorV2
        setup={makeSetup({ onSetColorScheme: vi.fn(), initialColorScheme: "dark" })}
      />,
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: /server selector settings/i }), {
      button: 0,
    });
    expect(screen.getByRole("menuitemradio", { name: "Dark" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });
});
