import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AddStaticSandboxDialog, RenameStaticSandboxDialog } from "./StaticSandboxDialogs";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      provider: "remote",
      sandbox_id: "foo",
      label: "Renamed",
      status: "running",
    }),
  } as unknown as Response);
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function wrap(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{node}</QueryClientProvider>;
}

describe("RenameStaticSandboxDialog", () => {
  it("PUTs the trimmed label and closes", async () => {
    const onOpenChange = vi.fn();
    render(
      wrap(
        <RenameStaticSandboxDialog
          target={{
            provider: "remote",
            id: "foo",
            label: "Checkout",
            status: "running",
            repo: "",
            lastActivityAt: null,
          }}
          onOpenChange={onOpenChange}
        />,
      ),
    );
    fireEvent.change(screen.getByTestId("rename-static-sandbox-label"), {
      target: { value: "  Renamed  " },
    });
    fireEvent.click(screen.getByTestId("rename-static-sandbox-save"));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/static-sandboxes/remote/foo");
    expect((init as RequestInit).method).toBe("PUT");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ label: "Renamed" });
  });
});

describe("AddStaticSandboxDialog", () => {
  it("scrolls a long VM list and shows long VM names in full", async () => {
    const longName = "a-very-long-static-sandbox-id-that-would-not-fit-on-one-line";
    const vms = [longName, ...Array.from({ length: 30 }, (_, i) => `vm-${i}`)].map((ws) => ({
      provider: "remote",
      sandbox_id: ws,
      status: "running",
      repo: "example/sample-app",
      last_activity_at: null,
    }));
    fetchMock.mockImplementation(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () =>
        url.endsWith("/available") ? { available_sandboxes: vms } : { static_sandboxes: [] },
    }));
    render(wrap(<AddStaticSandboxDialog open onOpenChange={vi.fn()} onAdded={vi.fn()} />));
    await screen.findByTestId("add-static-sandbox-option-vm-29");
    expect(screen.getByTestId("add-static-sandbox-dialog").className).toContain("max-h-");
    expect(screen.getByTestId("add-static-sandbox-body").className).toContain("overflow-y-auto");
    const name = screen.getByText(longName);
    expect(name.className).toContain("break-all");
    expect(name.className).not.toContain("truncate");
  });

  it("saves the pick at its own provider and names the server's provider", async () => {
    const calls: [string, RequestInit | undefined][] = [];
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      const body = url.endsWith("/available")
        ? { available_sandboxes: [{ provider: "other", sandbox_id: "bar", status: "running" }] }
        : url === "/v1/static-sandboxes"
          ? { static_sandboxes: [] }
          : { provider: "other", sandbox_id: "bar", label: "bar", status: "running" };
      return { ok: true, status: 200, json: async () => body };
    });
    const onAdded = vi.fn();
    render(
      wrap(
        <AddStaticSandboxDialog open onOpenChange={vi.fn()} onAdded={onAdded} provider="remote" />,
      ),
    );
    expect(screen.getByText("Add a Remote Static Sandbox")).toBeTruthy();
    fireEvent.click(await screen.findByTestId("add-static-sandbox-option-bar"));
    fireEvent.click(screen.getByTestId("add-static-sandbox-confirm"));
    await waitFor(() => expect(onAdded).toHaveBeenCalledWith("bar"));
    const put = calls.find(([, init]) => init?.method === "PUT");
    expect(put?.[0]).toBe("/v1/static-sandboxes/other/bar");
  });
});
