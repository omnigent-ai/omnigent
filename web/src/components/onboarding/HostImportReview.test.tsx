import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Host } from "@/hooks/useHosts";

const authenticatedFetchMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: authenticatedFetchMock }));
vi.mock("@/lib/nativeBridge", () => ({ isIOSShell: () => false }));

import { ImportReviewGate, ReviewImportsPanel } from "./HostImportReview";

function host(id: string, overrides: Partial<Host> = {}): Host {
  return {
    host_id: id,
    name: `${id}-machine`,
    owner: "me",
    status: "online",
    configured_harnesses: { "claude-native": true },
    ...overrides,
  };
}

/** Serve hosts plus per-host skill names; MCP inventories are empty. */
function serve(hosts: Host[], skillsByHost: Record<string, string[]>) {
  authenticatedFetchMock.mockImplementation(async (url: string) => {
    const parsed = new URL(url, "http://test");
    if (parsed.pathname === "/v1/hosts") return Response.json({ hosts });
    if (parsed.pathname === "/v1/skills") {
      const names = skillsByHost[parsed.searchParams.get("host_id") ?? ""] ?? [];
      return Response.json({ skills: names.map((name) => ({ name, description: "" })) });
    }
    if (parsed.pathname.endsWith("/mcp-servers")) return Response.json({ mcp_servers: [] });
    throw new Error(`unexpected request ${url}`);
  });
}

function renderWithClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  authenticatedFetchMock.mockReset();
  window.localStorage.clear();
});

afterEach(cleanup);

describe("ImportReviewGate", () => {
  it("opens once for a new host and remembers the review", async () => {
    serve([host("a")], { a: ["review"] });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText("Your imports are ready")).toBeTruthy();
    expect(screen.getByText("/review")).toBeTruthy();
    // A single host isn't named.
    expect(screen.queryByText(/on a-machine/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(screen.queryByText("Your imports are ready")).toBeNull());
    expect(window.localStorage.getItem("omnigent:imports-reviewed:a")).not.toBeNull();

    cleanup();
    renderWithClient(<ImportReviewGate />);
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalled());
    expect(screen.queryByText("Your imports are ready")).toBeNull();
  });

  it("skips offline, reviewed, and empty hosts, and names the host among several", async () => {
    window.localStorage.setItem("omnigent:imports-reviewed:reviewed", "x");
    serve(
      [host("offline", { status: "offline" }), host("reviewed"), host("empty"), host("fresh")],
      { offline: ["a"], reviewed: ["b"], fresh: ["c"] },
    );
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText(/Found in your harnesses on fresh-machine\./)).toBeTruthy();
    expect(screen.getByText("/c")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByText("Your imports are ready")).toBeNull());
    expect(window.localStorage.getItem("omnigent:imports-reviewed:fresh")).not.toBeNull();
    expect(window.localStorage.getItem("omnigent:imports-reviewed:empty")).toBeNull();
  });
});

describe("ReviewImportsPanel", () => {
  it("reopens the modal for a reviewed host", async () => {
    window.localStorage.setItem("omnigent:imports-reviewed:a", "x");
    serve([host("a")], { a: ["review"] });
    renderWithClient(<ReviewImportsPanel />);

    fireEvent.click(await screen.findByRole("button", { name: "Review imports on a-machine" }));
    expect(await screen.findByText("/review")).toBeTruthy();
  });

  it("explains when no machine is online", async () => {
    serve([host("a", { status: "offline" })], {});
    renderWithClient(<ReviewImportsPanel />);
    expect(await screen.findByText(/None of your machines are online/)).toBeTruthy();
  });
});
