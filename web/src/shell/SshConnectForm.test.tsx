import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchHosts, type Host } from "@/hooks/useHosts";
import { SshConnectForm } from "./SshConnectForm";

vi.mock("@/hooks/useHosts", () => ({ fetchHosts: vi.fn(async () => []) }));

function online(...hostIds: string[]): Host[] {
  return hostIds.map((hostId) => ({ host_id: hostId, status: "online" }) as Host);
}

interface Desktop {
  kind: "electron";
  connectSshHost: ReturnType<typeof vi.fn>;
}

function renderForm(result: Record<string, unknown>) {
  const desktop: Desktop = {
    kind: "electron",
    connectSshHost: vi.fn().mockResolvedValue(result),
  };
  (window as unknown as { omnigentDesktop?: Desktop }).omnigentDesktop = desktop;
  const queryClient = new QueryClient();
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const onConnected = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <SshConnectForm onConnected={onConnected} />
    </QueryClientProvider>,
  );
  return { desktop, invalidate, onConnected };
}

function connect(destination: string) {
  fireEvent.change(screen.getByTestId("ssh-connect-destination"), {
    target: { value: destination },
  });
  fireEvent.click(screen.getByTestId("ssh-connect-submit"));
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.mocked(fetchHosts).mockReset().mockResolvedValue([]);
  delete (window as unknown as { omnigentDesktop?: Desktop }).omnigentDesktop;
});

describe("SshConnectForm", () => {
  it("connects the trimmed host and selects it", async () => {
    const { desktop, invalidate, onConnected } = renderForm({
      ok: true,
      identity: { serverUrl: "https://example.com/omnigent", hostId: "h1" },
    });

    connect("  my-ec2 ");

    await waitFor(() => expect(onConnected).toHaveBeenCalledWith("h1"));
    expect(desktop.connectSshHost).toHaveBeenCalledWith("my-ec2");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["hosts"] });
  });

  it("stays quiet when the desktop window already showed the outcome or was closed", async () => {
    for (const result of [
      { ok: false, shownInConsole: true, error: "my-ec2 didn't accept your sign-in." },
      { ok: false, canceled: true, error: "Connecting over SSH wasn't approved." },
    ]) {
      const { desktop, onConnected } = renderForm(result);
      connect("my-ec2");
      // Sequential by design: each case renders and settles before the next.
      // oxlint-disable-next-line no-await-in-loop
      await waitFor(() => expect(desktop.connectSshHost).toHaveBeenCalled());
      expect(screen.queryByTestId("ssh-connect-message")).toBeNull();
      expect(onConnected).not.toHaveBeenCalled();
      cleanup();
    }
  });

  it("keeps the dialog open to say a machine was already connected", async () => {
    const { onConnected } = renderForm({ ok: true, alreadyRunning: true, identity: null });

    connect("my-ec2");

    expect(await screen.findByTestId("ssh-connect-message")).toHaveTextContent(
      "my-ec2 was already connected to this server.",
    );
    expect(onConnected).not.toHaveBeenCalled();
  });

  it("selects the host that came online when the shell couldn't read its id", async () => {
    // Before the connect, then once the new machine registered.
    vi.mocked(fetchHosts).mockResolvedValueOnce(online("h1")).mockResolvedValue(online("h1", "h2"));
    const { onConnected } = renderForm({ ok: true, alreadyRunning: false, identity: null });

    connect("my-ec2");

    await waitFor(() => expect(onConnected).toHaveBeenCalledWith("h2"));
  });

  it("stays open, keeping the previous host, when the new one never appears", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(fetchHosts).mockResolvedValue(online("h1"));
    const { onConnected } = renderForm({ ok: true, alreadyRunning: false, identity: null });

    connect("my-ec2");
    await vi.advanceTimersByTimeAsync(31_000);

    expect(await screen.findByTestId("ssh-connect-message")).toHaveTextContent(
      "my-ec2 is connected, but its host hasn't appeared yet.",
    );
    expect(onConnected).not.toHaveBeenCalled();
  });

  it("shows a failure that has no other surface", async () => {
    renderForm({ ok: false, error: "Enter a host from your SSH config." });

    connect("bad host");

    expect(await screen.findByTestId("ssh-connect-message")).toHaveTextContent(
      "Enter a host from your SSH config.",
    );
  });
});
