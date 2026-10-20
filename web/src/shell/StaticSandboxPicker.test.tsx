import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { useViewerId } from "@/hooks/useViewerId";
import { StaticSandboxState } from "./StaticSandboxPicker";

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: vi.fn(() => "alice") }));

const host: Host = {
  host_id: "host_one",
  name: "My box",
  owner: "alice",
  status: "offline",
  sandbox_attached: true,
  sandbox_provider: "static",
  sandbox_id: "box",
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("shares inventory per provider, skips closed pickers, and isolates owners and providers", async () => {
  const client = new QueryClient();
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(
      async () =>
        new Response(JSON.stringify({ sandboxes: [{ sandbox_id: "box", state: "running" }] })),
    );
  const rows = (enabled: boolean, second = "static") => (
    <QueryClientProvider client={client}>
      <StaticSandboxState host={host} enabled={enabled} />
      <StaticSandboxState
        host={{ ...host, host_id: "host_two", sandbox_provider: second }}
        enabled={enabled}
      />
    </QueryClientProvider>
  );
  const { rerender } = render(rows(false));
  expect(fetch).not.toHaveBeenCalled();
  rerender(rows(true));
  await waitFor(() => expect(screen.getAllByText("Sandbox: running")).toHaveLength(2));
  expect(fetch).toHaveBeenCalledTimes(1);
  rerender(rows(false));
  rerender(rows(true));
  expect(fetch).toHaveBeenCalledTimes(1);
  fetch.mockImplementation(() => new Promise(() => {}));
  rerender(rows(true, "other"));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  expect(screen.getAllByText("Sandbox: running")).toHaveLength(1);
  vi.mocked(useViewerId).mockReturnValueOnce("bob").mockReturnValueOnce("bob");
  rerender(rows(true, "other"));
  expect(screen.queryByText("Sandbox: running")).not.toBeInTheDocument();
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
});

it.each([null, undefined, "", "missing"])(
  "leaves unknown inventory state (%s) unset",
  async (state) => {
    const client = new QueryClient();
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          sandboxes: state === "missing" ? [] : [{ sandbox_id: "box", state }],
        }),
      ),
    );
    const { container } = render(
      <QueryClientProvider client={client}>
        <StaticSandboxState host={host} enabled />
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(client.getQueryState(["sandbox-discovery", "alice", "static"])?.status).toBe(
        "success",
      ),
    );
    expect(container).toBeEmptyDOMElement();
    expect(fetch).toHaveBeenCalledTimes(1);
  },
);

it("marks stale state and replaces it with unavailable on failure, then recovers", async () => {
  const client = new QueryClient();
  const key = ["sandbox-discovery", "alice", "static"];
  client.setQueryData(key, [{ sandbox_id: "box", state: "stopped" }], {
    updatedAt: Date.now() - 60_000,
  });
  let finish!: (response: Response) => void;
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  render(
    <QueryClientProvider client={client}>
      <StaticSandboxState host={host} enabled />
    </QueryClientProvider>,
  );
  expect(screen.getByText("Last reported: stopped")).toBeVisible();
  await act(async () => {
    finish(new Response("unavailable", { status: 503 }));
  });
  expect(await screen.findByText("State unavailable")).toBeVisible();
  expect(screen.queryByText(/stopped/)).not.toBeInTheDocument();
  fetch.mockResolvedValue(
    new Response(JSON.stringify({ sandboxes: [{ sandbox_id: "box", state: "running" }] })),
  );
  await act(async () => {
    await client.invalidateQueries({ queryKey: ["sandbox-discovery"] });
  });
  expect(await screen.findByText("Sandbox: running")).toBeVisible();
});

it("bounds slow lookups and preserves unknown state for older host responses", async () => {
  let timeout!: AbortController;
  const timeoutSignal = vi.spyOn(AbortSignal, "timeout").mockImplementation(() => {
    timeout = new AbortController();
    return timeout.signal;
  });
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(
    (_url, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("Timed out", "TimeoutError")),
          { once: true },
        );
      }),
  );
  const client = new QueryClient();
  const row = (value: Host) => (
    <QueryClientProvider client={client}>
      <StaticSandboxState host={value} enabled />
    </QueryClientProvider>
  );
  const { rerender } = render(row({ ...host, sandbox_id: undefined }));
  expect(fetch).not.toHaveBeenCalled();
  rerender(row({ ...host, sandbox_attached: false }));
  expect(fetch).not.toHaveBeenCalled();
  rerender(row(host));
  expect(timeoutSignal).toHaveBeenCalledWith(5_000);
  await act(async () => {
    timeout.abort();
  });
  expect(await screen.findByText("State unavailable")).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(1);
});
