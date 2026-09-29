import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

function openPresetDropdown() {
  fireEvent.pointerDown(screen.getByRole("button", { name: /choose team url/i }), { button: 0 });
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

  it("a returning MDM user starts on the landing, and Join opens the preset directly", async () => {
    const onConnect = vi.fn().mockResolvedValue({});
    const getRunnerOptions = vi.fn().mockResolvedValue({ remote: true });
    render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: true,
          connectedBefore: true,
          managedServers: ["https://team.example.com/"],
          onConnect,
          getRunnerOptions,
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    await waitFor(() => expect(onConnect).toHaveBeenCalledWith("https://team.example.com/"));
    expect(getRunnerOptions).not.toHaveBeenCalled();
  });

  it("an MDM landing shows a direct connect's error, and a failed load's", async () => {
    const onConnect = vi.fn().mockResolvedValue({ error: "rejected" });
    const { unmount } = render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: true,
          recentServers: ["https://old.example.com/"],
          managedServers: ["https://team.example.com/"],
          onConnect,
        })}
      />,
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: /choose team url/i }), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "old.example.com" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("rejected");
    unmount();

    render(
      <ServerSelectorV2
        setup={makeSetup({
          error: "Could not load",
          managedServers: ["https://team.example.com/"],
        })}
      />,
    );
    expect(screen.getByRole("heading", { name: "Meet Omnigent" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load");
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

  it("a new MDM user's typed URL goes through the runner step", async () => {
    const getRunnerOptions = vi.fn().mockResolvedValue({ remote: false });
    render(
      <ServerSelectorV2
        setup={makeSetup({ managedServers: ["https://team.example.com/"], getRunnerOptions })}
      />,
    );
    fireEvent.pointerDown(screen.getByRole("button", { name: /choose team url/i }), { button: 0 });
    fireEvent.change(screen.getByLabelText("Server URL"), {
      target: { value: "https://typed.example.com" },
    });
    fireEvent.keyDown(screen.getByLabelText("Server URL"), { key: "Enter" });
    expect(
      await screen.findByRole("heading", { name: /where do you work today/i }),
    ).toBeInTheDocument();
    expect(getRunnerOptions).toHaveBeenCalledWith("https://typed.example.com/");
  });

  it("picking a preset offers only this laptop when the shell reports no remote environment", async () => {
    const getRunnerOptions = vi.fn().mockResolvedValue({ remote: false });
    render(
      <ServerSelectorV2
        setup={makeSetup({ managedServers: ["https://team.example.com/"], getRunnerOptions })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    expect(
      await screen.findByRole("heading", { name: /where do you work today/i }),
    ).toBeInTheDocument();
    expect(getRunnerOptions).toHaveBeenCalledWith("https://team.example.com/");
    fireEvent.click(screen.getByRole("combobox", { name: "Runner" }));
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["My laptop"]);
  });

  it("a slow runner lookup can't replace a newer pick", async () => {
    let resolveFirst: (v: { remote: boolean }) => void = () => {};
    const getRunnerOptions = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockResolvedValue({ remote: false });
    const onConnect = vi.fn().mockResolvedValue({});
    render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: true,
          managedServers: ["https://team.example.com/"],
          getRunnerOptions,
          onConnect,
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    openPresetDropdown();
    fireEvent.change(screen.getByLabelText("Server URL"), {
      target: { value: "https://typed.example.com" },
    });
    fireEvent.keyDown(screen.getByLabelText("Server URL"), { key: "Enter" });
    const runner = await screen.findByRole("combobox", { name: "Runner" });
    resolveFirst({ remote: true });
    await waitFor(() => expect(getRunnerOptions).toHaveBeenCalledTimes(2));
    expect(runner).toHaveTextContent("My laptop");
    fireEvent.click(screen.getByRole("button", { name: "Open Omnigent" }));
    await waitFor(() => expect(onConnect).toHaveBeenCalledWith("https://typed.example.com/"));
  });

  it("a failed runner lookup still opens the runner step, laptop only", async () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({
          managedServers: ["https://team.example.com/"],
          getRunnerOptions: vi.fn().mockRejectedValue(new Error("ipc down")),
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    fireEvent.click(await screen.findByRole("combobox", { name: "Runner" }));
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["My laptop"]);
  });

  it("the runner step shows a direct connect's error", async () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: true,
          managedServers: ["https://team.example.com/"],
          onConnect: vi.fn().mockResolvedValue({ error: "unreachable" }),
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Open Omnigent" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("unreachable");
  });

  it("defaults the runner to the remote environment when offered", async () => {
    render(
      <ServerSelectorV2
        setup={makeSetup({
          managedServers: ["https://team.example.com/"],
          getRunnerOptions: vi.fn().mockResolvedValue({ remote: true }),
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    const runner = await screen.findByRole("combobox", { name: "Runner" });
    expect(runner).toHaveTextContent("Arca");
    fireEvent.click(runner);
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["Arca", "My laptop"]);
  });

  it("the runner step connects to the preset, and Back returns to the landing", async () => {
    const onConnect = vi.fn().mockResolvedValue({});
    const { unmount } = render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: true,
          managedServers: ["https://team.example.com/"],
          onConnect,
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Open Omnigent" }));
    await waitFor(() => expect(onConnect).toHaveBeenCalledWith("https://team.example.com/"));
    unmount();

    render(
      <ServerSelectorV2 setup={makeSetup({ managedServers: ["https://team.example.com/"] })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(screen.getByRole("heading", { name: "Meet Omnigent" })).toBeInTheDocument();
  });

  async function installFromRunnerStep(over: Partial<ServerSelectorV2Setup>, pick?: string) {
    render(
      <ServerSelectorV2
        setup={makeSetup({ managedServers: ["https://team.example.com/"], ...over })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    if (pick) {
      fireEvent.click(await screen.findByRole("combobox", { name: "Runner" }));
      fireEvent.click(screen.getByRole("option", { name: pick }));
    }
    fireEvent.click(await screen.findByRole("button", { name: /(install|open) omnigent/i }));
  }

  it("a remote runner connects first, streaming its output, then opens the server, with no local install", async () => {
    let finishRunner: (v: { ok: boolean }) => void = () => {};
    let emit: (line: string) => void = () => {};
    const onConnectRunner = vi.fn(
      () =>
        new Promise<{ ok: boolean }>((resolve) => {
          finishRunner = resolve;
        }),
    );
    const onInstallCli = vi.fn().mockResolvedValue({ ok: true });
    const onConnect = vi.fn().mockResolvedValue({});
    await installFromRunnerStep({
      installed: false,
      onInstallCli,
      onConnectRunner,
      onConnect,
      onRunnerLog: (cb) => {
        emit = cb;
        return () => {};
      },
      getRunnerOptions: vi.fn().mockResolvedValue({ remote: true, bundledCli: true }),
    });
    expect(screen.getByText(/connecting your remote environment/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(onConnectRunner).toHaveBeenCalledWith("https://team.example.com/", "remote"),
    );
    act(() => emit("$ remote host --server https://team.example.com/"));
    expect(
      await screen.findByText("$ remote host --server https://team.example.com/"),
    ).toBeInTheDocument();
    expect(onConnect).not.toHaveBeenCalled();
    finishRunner({ ok: true });
    await waitFor(() => expect(onConnect).toHaveBeenCalledWith("https://team.example.com/"));
    expect(onInstallCli).not.toHaveBeenCalled();
  });

  it("tells a laptop runner, and only a laptop runner, what connecting grants", async () => {
    const grant = "The server will be able to run agents on this laptop.";
    render(
      <ServerSelectorV2
        setup={makeSetup({
          managedServers: ["https://team.example.com/"],
          getRunnerOptions: vi.fn().mockResolvedValue({ remote: true }),
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    // Arca is the default: no laptop grant to explain.
    fireEvent.click(await screen.findByRole("combobox", { name: "Runner" }));
    expect(screen.queryByText(grant)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: "My laptop" }));
    expect(screen.getByText(grant)).toBeInTheDocument();
  });

  it("the laptop skips the install when its host CLI is bundled", async () => {
    const onConnectRunner = vi.fn().mockResolvedValue({ ok: true });
    const onInstallCli = vi.fn().mockResolvedValue({ ok: true });
    const onConnect = vi.fn().mockResolvedValue({});
    await installFromRunnerStep(
      {
        installed: false,
        onInstallCli,
        onConnectRunner,
        onConnect,
        getRunnerOptions: vi.fn().mockResolvedValue({ remote: true, bundledCli: true }),
      },
      "My laptop",
    );
    await waitFor(() => expect(onConnect).toHaveBeenCalledOnce());
    expect(onConnectRunner).toHaveBeenCalledWith("https://team.example.com/", "local");
    expect(onInstallCli).not.toHaveBeenCalled();
  });

  it("the laptop installs a missing CLI before connecting", async () => {
    const calls: string[] = [];
    const onInstallCli = vi.fn(async () => {
      calls.push("install");
      return { ok: true };
    });
    const onConnectRunner = vi.fn(async () => {
      calls.push("runner");
      return { ok: true };
    });
    const onConnect = vi.fn(async () => {
      calls.push("connect");
      return {};
    });
    await installFromRunnerStep({ installed: false, onInstallCli, onConnectRunner, onConnect });
    await waitFor(() => expect(calls).toEqual(["install", "runner", "connect"]));
    expect(onConnectRunner).toHaveBeenCalledWith("https://team.example.com/", "local");
  });

  it("a failed runner connect shows the error, doesn't open the server, and Back returns to the runner step", async () => {
    const onConnect = vi.fn().mockResolvedValue({});
    await installFromRunnerStep({
      installed: true,
      onConnect,
      onConnectRunner: vi.fn().mockResolvedValue({ ok: false, error: "no remote host" }),
      getRunnerOptions: vi.fn().mockResolvedValue({ remote: true }),
    });
    expect(await screen.findByText(/no remote host/)).toBeInTheDocument();
    expect(onConnect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByRole("heading", { name: /where do you work today/i })).toBeInTheDocument();
  });

  // Whatever the shell reports, the user can always type an arbitrary server URL.
  it.each<[string, Partial<ServerSelectorV2Setup>, () => void]>([
    [
      "new, no MDM",
      {},
      () => fireEvent.click(screen.getByRole("button", { name: /join your team/i })),
    ],
    ["new, MDM presets", { managedServers: ["https://team.example.com/"] }, openPresetDropdown],
    [
      "returning, recents",
      { recentServers: ["https://team.example.com/"] },
      () => fireEvent.click(screen.getByRole("button", { name: "Add server" })),
    ],
    [
      "returning, MDM presets",
      { connectedBefore: true, managedServers: ["https://team.example.com/"] },
      openPresetDropdown,
    ],
    [
      "Connect to new server…, MDM",
      { initialStep: "server", managedServers: ["https://team.example.com/"] },
      openPresetDropdown,
    ],
    [
      "failed connect, MDM",
      { error: "Could not load http://dead/", managedServers: ["https://team.example.com/"] },
      openPresetDropdown,
    ],
    ["failed connect, no servers", { error: "Could not load http://dead/" }, () => {}],
  ])("reaches a server URL input: %s", (_name, over, open) => {
    render(<ServerSelectorV2 setup={makeSetup(over)} />);
    open();
    expect(screen.getByLabelText("Server URL")).toBeInTheDocument();
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
