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

  it.each([true, false])(
    "a returning MDM user opens the preset directly (CLI installed: %s)",
    async (installed) => {
      const onConnect = vi.fn().mockResolvedValue({});
      const onInstallCli = vi.fn().mockResolvedValue({ ok: true });
      const getRunnerOptions = vi.fn().mockResolvedValue({ remote: true });
      render(
        <ServerSelectorV2
          setup={makeSetup({
            installed,
            connectedBefore: true,
            managedServers: ["https://team.example.com/"],
            onConnect,
            onInstallCli,
            getRunnerOptions,
          })}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
      await waitFor(() =>
        expect(onConnect).toHaveBeenCalledWith("https://team.example.com/", expect.any(Function)),
      );
      expect(getRunnerOptions).not.toHaveBeenCalled();
      expect(onInstallCli).not.toHaveBeenCalled();
    },
  );

  it("opens a recent remote server without installing the local CLI", async () => {
    const onConnect = vi.fn().mockResolvedValue({});
    const onInstallCli = vi.fn().mockResolvedValue({ ok: true });
    render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: false,
          recentServers: ["https://team.example.com/"],
          onConnect,
          onInstallCli,
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Open Omnigent" }));
    await waitFor(() =>
      expect(onConnect).toHaveBeenCalledWith("https://team.example.com/", expect.any(Function)),
    );
    expect(onInstallCli).not.toHaveBeenCalled();
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
    await waitFor(() =>
      expect(onConnect).toHaveBeenCalledWith("http://localhost:6767/", expect.any(Function)),
    );
    expect(onStartLocal).not.toHaveBeenCalled();
  });

  it("still installs the CLI when explicitly opening a local installation", async () => {
    const onInstallCli = vi.fn().mockResolvedValue({ ok: true });
    const onStartLocal = vi.fn().mockResolvedValue({ ok: true });
    render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: false,
          recentServers: ["http://localhost:6767/"],
          onInstallCli,
          onStartLocal,
          onCheckServer: vi.fn().mockResolvedValue({ status: "unreachable" }),
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Install Omnigent" }));
    await waitFor(() => expect(onStartLocal).toHaveBeenCalledOnce());
    expect(onInstallCli).toHaveBeenCalledOnce();
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
      await waitFor(() => expect(onConnect).toHaveBeenCalledWith(url, expect.any(Function)));
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
    expect(screen.getAllByRole("radio")).toEqual([
      screen.getByRole("radio", { name: /my laptop/i }),
    ]);
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
    const laptop = await screen.findByRole("radio", { name: /my laptop/i });
    resolveFirst({ remote: true });
    await waitFor(() => expect(getRunnerOptions).toHaveBeenCalledTimes(2));
    expect(laptop).toBeChecked();
    expect(screen.queryByRole("radio", { name: "Arca" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open Omnigent" }));
    await waitFor(() =>
      expect(onConnect).toHaveBeenCalledWith("https://typed.example.com/", expect.any(Function)),
    );
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
    expect(await screen.findByRole("radio", { name: /my laptop/i })).toBeChecked();
    expect(screen.getAllByRole("radio")).toHaveLength(1);
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
    expect(await screen.findByRole("radio", { name: "Arca" })).toBeChecked();
    expect(screen.getByRole("radio", { name: /my laptop/i })).not.toBeChecked();
    expect(screen.getAllByRole("radio")).toHaveLength(2);
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
    await waitFor(() =>
      expect(onConnect).toHaveBeenCalledWith("https://team.example.com/", expect.any(Function)),
    );
    unmount();

    render(
      <ServerSelectorV2 setup={makeSetup({ managedServers: ["https://team.example.com/"] })} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(screen.getByRole("heading", { name: "Meet Omnigent" })).toBeInTheDocument();
  });

  async function installFromRunnerStep(
    over: Partial<ServerSelectorV2Setup>,
    pick?: string | RegExp,
    action: string | RegExp = /(install|open) omnigent/i,
  ) {
    render(
      <ServerSelectorV2
        setup={makeSetup({ managedServers: ["https://team.example.com/"], ...over })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    if (pick) {
      fireEvent.click(await screen.findByRole("radio", { name: pick }));
    }
    fireEvent.click(await screen.findByRole("button", { name: action }));
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
    expect(screen.queryByRole("button", { name: "Continue anyway" })).not.toBeInTheDocument();
    await waitFor(() =>
      expect(onConnectRunner).toHaveBeenCalledWith("https://team.example.com/", "remote"),
    );
    act(() => emit("$ remote host --server https://team.example.com/"));
    expect(
      await screen.findByText("$ remote host --server https://team.example.com/"),
    ).toBeInTheDocument();
    expect(onConnect).not.toHaveBeenCalled();
    finishRunner({ ok: true });
    await waitFor(() =>
      expect(onConnect).toHaveBeenCalledWith("https://team.example.com/", expect.any(Function)),
    );
    expect(onInstallCli).not.toHaveBeenCalled();
  });

  it("can retry Arca or Continue anyway after Arca startup fails", async () => {
    const onConnectRunner = vi.fn().mockResolvedValue({ ok: false, error: "Arca failed" });
    const onInstallCli = vi.fn();
    let finishConnect: (result: { error?: string }) => void = () => {};
    const onConnect = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<{ error?: string }>((resolve) => {
            finishConnect = resolve;
          }),
      )
      .mockResolvedValue({});
    await installFromRunnerStep({
      installed: false,
      onInstallCli,
      onConnectRunner,
      onConnect,
      getRunnerOptions: vi.fn().mockResolvedValue({ remote: true, bundledCli: true }),
    });
    expect(await screen.findByText("Arca failed")).toBeInTheDocument();
    expect(onConnect).not.toHaveBeenCalled();
    const skip = screen.getByRole("button", { name: "Continue anyway" });
    expect(skip.nextElementSibling).toBe(screen.getByRole("button", { name: "Retry" }));

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Arca failed")).toBeInTheDocument();
    expect(onConnectRunner).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Continue anyway" }));
    await waitFor(() =>
      expect(onConnect).toHaveBeenCalledWith("https://team.example.com/", expect.any(Function)),
    );
    expect(screen.queryByRole("button", { name: "Continue anyway" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.queryByText("Arca failed")).not.toBeInTheDocument();

    act(() => finishConnect({ error: "Server unavailable" }));
    expect(await screen.findByText("Server unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue anyway" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Server ready")).toBeInTheDocument();
    expect(onConnect).toHaveBeenCalledTimes(2);
    expect(onConnectRunner).toHaveBeenCalledTimes(2);
    expect(onInstallCli).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Continue anyway" })).not.toBeInTheDocument();
  });

  it.each(["install", "laptop", "server"])(
    "does not offer Continue anyway after a %s failure",
    async (failure) => {
      await installFromRunnerStep({
        installed: false,
        onInstallCli: vi.fn().mockResolvedValue({ ok: failure !== "install", error: "failed" }),
        onConnectRunner: vi.fn().mockResolvedValue({ ok: failure !== "laptop", error: "failed" }),
        onConnect: vi.fn().mockResolvedValue({ error: "failed" }),
        getRunnerOptions: vi.fn().mockResolvedValue({ remote: failure === "server" }),
      });
      expect(await screen.findByText("failed")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Continue anyway" })).not.toBeInTheDocument();
    },
  );

  it("a cancelled connect in the terminal fails with Retry instead of reading ready", async () => {
    await installFromRunnerStep({
      installed: true,
      onConnectRunner: vi.fn().mockResolvedValue({ ok: true }),
      onConnect: vi.fn().mockResolvedValue({ cancelled: true }),
      getRunnerOptions: vi.fn().mockResolvedValue({ remote: true, bundledCli: true }),
    });
    expect(await screen.findByText("Connection cancelled.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByText(/server ready/i)).not.toBeInTheDocument();
  });

  // A connect held in browser sign-in until `finish` settles it.
  function pendingSignIn() {
    let finish: (r: { cancelled?: boolean }) => void = () => {};
    const onConnect = vi.fn(
      (_url: string, onPhase?: (phase: "connecting" | "authenticating") => void) =>
        new Promise<{ cancelled?: boolean }>((resolve) => {
          onPhase?.("authenticating");
          finish = resolve;
        }),
    );
    return { onConnect, finish: (r: { cancelled?: boolean }) => finish(r) };
  }

  function renderRecents(over: Partial<ServerSelectorV2Setup>) {
    render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: true,
          recentServers: ["https://team.example.com/"],
          ...over,
        })}
      />,
    );
    return screen.getByRole("button", { name: /open omnigent/i });
  }

  it("a direct connect shows its sign-in phase and Cancelling…, then goes idle", async () => {
    const { onConnect, finish } = pendingSignIn();
    let confirm: (cancelled: boolean) => void = () => {};
    const onCancelConnect = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          confirm = resolve;
        }),
    );
    const open = renderRecents({ onConnect, onCancelConnect });
    fireEvent.click(open);
    expect(await screen.findByText(/finish signing in in your browser/i)).toBeInTheDocument();
    expect(open).toHaveAttribute("aria-busy", "true");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancelConnect).toHaveBeenCalled();
    expect(screen.getByText("Cancelling…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    await act(async () => confirm(true));
    act(() => finish({ cancelled: true }));
    await waitFor(() => expect(screen.queryByText("Cancelling…")).not.toBeInTheDocument());
    expect(open).not.toHaveAttribute("aria-busy");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("a Cancel the shell refuses restores the phase and says why", async () => {
    const { onConnect } = pendingSignIn();
    const open = renderRecents({ onConnect, onCancelConnect: vi.fn().mockResolvedValue(false) });
    fireEvent.click(open);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/still finishing/i);
    expect(screen.getByText(/finish signing in in your browser/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(open).toHaveAttribute("aria-busy", "true");
  });

  it("a second join from the MDM dropdown can't start or hide the first's progress", async () => {
    const { onConnect, finish } = pendingSignIn();
    render(
      <ServerSelectorV2
        setup={makeSetup({
          installed: true,
          connectedBefore: true,
          managedServers: ["https://team.example.com/"],
          onConnect,
          onCancelConnect: vi.fn(),
        })}
      />,
    );
    openPresetDropdown();
    const input = await screen.findByRole("textbox", { name: "Server URL" });
    fireEvent.change(input, { target: { value: "https://other.example.com" } });
    // Both Enters land before the dropdown's close renders.
    act(() => {
      fireEvent.keyDown(input, { key: "Enter" });
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(onConnect).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Server URL" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/finish signing in in your browser/i)).toBeInTheDocument();
    act(() => finish({ cancelled: true }));
    await waitFor(() =>
      expect(screen.queryByText(/finish signing in in your browser/i)).not.toBeInTheDocument(),
    );
  });

  it("tells the laptop option what connecting grants", async () => {
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
    expect(await screen.findByRole("radio", { name: /my laptop/i })).toHaveAccessibleName(
      expect.stringContaining(grant),
    );
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
      /my laptop/i,
      // Nothing is installed, so the action only opens.
      "Open Omnigent",
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
    await installFromRunnerStep(
      { installed: false, onInstallCli, onConnectRunner, onConnect },
      undefined,
      "Install Omnigent",
    );
    await waitFor(() => expect(calls).toEqual(["install", "runner", "connect"]));
    expect(onConnectRunner).toHaveBeenCalledWith("https://team.example.com/", "local");
  });

  it("still installs for explicit laptop setup on shells without runner support", async () => {
    const onInstallCli = vi.fn().mockResolvedValue({ ok: true });
    const onConnect = vi.fn().mockResolvedValue({});
    await installFromRunnerStep({ installed: false, onInstallCli, onConnect });
    await waitFor(() => expect(onConnect).toHaveBeenCalledOnce());
    expect(onInstallCli).toHaveBeenCalledOnce();
    expect(onInstallCli.mock.invocationCallOrder[0]).toBeLessThan(
      onConnect.mock.invocationCallOrder[0],
    );
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

describe("onboarding parity", () => {
  afterEach(() => vi.restoreAllMocks());

  it.each([
    [{}, "Server ready"],
    [{ error: "Server unavailable" }, "Server unavailable"],
    [{ cancelled: true }, "Connection cancelled."],
  ])(
    "opens a selected local server without retrying failed installation (%j)",
    async (result, message) => {
      const setup = makeSetup({
        installed: false,
        recentServers: ["http://localhost:6767/"],
        onInstallCli: vi.fn().mockResolvedValue({ ok: false, error: "Installer failed" }),
        onConnect: vi.fn().mockResolvedValue(result),
      });
      render(<ServerSelectorV2 setup={setup} />);
      fireEvent.click(screen.getByRole("button", { name: "Install Omnigent" }));
      fireEvent.click(await screen.findByRole("button", { name: "Continue anyway" }));
      await screen.findByText(message);
      expect(setup.onConnect).toHaveBeenCalledWith("http://localhost:6767/", expect.any(Function));
      expect(setup.onInstallCli).toHaveBeenCalledOnce();
      expect(setup.onStartLocal).not.toHaveBeenCalled();
    },
  );

  it("does not offer Continue anyway when new local setup has no server destination", async () => {
    const setup = makeSetup({
      installed: false,
      onInstallCli: vi.fn().mockResolvedValue({ ok: false, error: "Installer failed" }),
    });
    render(<ServerSelectorV2 setup={setup} />);
    fireEvent.click(screen.getByRole("button", { name: /get started locally/i }));
    fireEvent.click(screen.getByRole("button", { name: "Install Omnigent" }));
    await screen.findByText("Installer failed");
    expect(screen.queryByRole("button", { name: "Continue anyway" })).not.toBeInTheDocument();
  });

  it("requires confirmation for remote HTTP and honours cancellation", async () => {
    const confirm = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    const setup = makeSetup({ recentServers: ["http://team.example.com/"] });
    render(<ServerSelectorV2 setup={setup} />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open Omnigent" })));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("unencrypted HTTP"));
    expect(setup.onConnect).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open Omnigent" })));
    expect(setup.onConnect).toHaveBeenCalledWith("http://team.example.com/", expect.any(Function));
  });

  it("asks before enrolling a runner on remote HTTP", async () => {
    const confirm = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    const setup = makeSetup({
      installed: true,
      managedServers: ["http://team.example.com/"],
      onConnectRunner: vi.fn().mockResolvedValue({ ok: true }),
    });
    render(<ServerSelectorV2 setup={setup} />);
    fireEvent.click(screen.getByRole("button", { name: /join your team/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Open Omnigent" }));
    await screen.findByText("Connection cancelled.");
    expect(setup.onConnectRunner).not.toHaveBeenCalled();
    expect(setup.onConnect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(setup.onConnect).toHaveBeenCalledOnce());
    expect(setup.onConnectRunner).toHaveBeenCalledOnce();
    expect(confirm).toHaveBeenCalledTimes(2);
  });

  it.each(["https://team.example.com/", "http://localhost:8000/", "http://[::1]:8000/"])(
    "does not warn for %s",
    async (url) => {
      const confirm = vi.spyOn(window, "confirm");
      const setup = makeSetup({ installed: true, recentServers: [url] });
      render(<ServerSelectorV2 setup={setup} />);
      fireEvent.click(screen.getByRole("button", { name: "Open Omnigent" }));
      await waitFor(() => expect(setup.onConnect).toHaveBeenCalledOnce());
      expect(confirm).not.toHaveBeenCalled();
    },
  );

  it("can open an existing local server from manual installation instructions", async () => {
    const setup = makeSetup({
      installed: false,
      installSupported: false,
      recentServers: ["http://localhost:6767/"],
    });
    render(<ServerSelectorV2 setup={setup} />);
    fireEvent.click(screen.getByRole("button", { name: "Install Omnigent" }));
    expect(screen.getByRole("link", { name: "Installation instructions" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Continue anyway" }));
    await waitFor(() => expect(setup.onConnect).toHaveBeenCalledOnce());
    expect(setup.onStartLocal).not.toHaveBeenCalled();
  });
});
