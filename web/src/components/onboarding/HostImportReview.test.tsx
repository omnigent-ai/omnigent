import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Host } from "@/hooks/useHosts";
import { clearImportReviewRequest, requestImportReview } from "@/lib/importReviewState";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { CapabilitiesContext } from "@/lib/CapabilitiesContext";

const authenticatedFetchMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: authenticatedFetchMock }));
vi.mock("@/lib/nativeBridge", () => ({ isIOSShell: () => false }));

import { ImportReviewGate } from "./HostImportReview";

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
function serve(
  hosts: Host[],
  skillsByHost: Record<string, string[]>,
  { absentSkillCalls = 0 }: { absentSkillCalls?: number } = {},
) {
  let absent = absentSkillCalls;
  authenticatedFetchMock.mockImplementation(async (url: string) => {
    const parsed = new URL(url, "http://test");
    if (parsed.pathname === "/v1/hosts") return Response.json({ hosts });
    if (parsed.pathname === "/v1/skills") {
      if (absent > 0) {
        absent -= 1;
        return Response.json({ detail: "host not connected" }, { status: 409 });
      }
      const names = skillsByHost[parsed.searchParams.get("host_id") ?? ""] ?? [];
      return Response.json({ skills: names.map((name) => ({ name, description: "" })) });
    }
    if (parsed.pathname.endsWith("/mcp-servers")) return Response.json({ mcp_servers: [] });
    throw new Error(`unexpected request ${url}`);
  });
}

// The import modal is gated behind the `import_review` release feature, so the
// gate renders only when the server advertises it.
const IMPORT_REVIEW_ON: ServerInfo = {
  ...FALLBACK_SERVER_INFO,
  features: { import_review: true },
};

function renderWithClient(ui: ReactNode, info: ServerInfo | "loading" = IMPORT_REVIEW_ON) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <CapabilitiesContext.Provider value={info}>{ui}</CapabilitiesContext.Provider>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

function skillRequestsFor(hostId: string) {
  return authenticatedFetchMock.mock.calls.filter(([url]) =>
    String(url).startsWith(`/v1/skills?host_id=${hostId}&`),
  );
}

beforeEach(() => {
  authenticatedFetchMock.mockReset();
  window.localStorage.clear();
  clearImportReviewRequest();
});

afterEach(cleanup);

describe("ImportReviewGate", () => {
  it("opens once for a new host and remembers the review", async () => {
    serve([host("a")], { a: ["review"] });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText("Your setup is ready")).toBeTruthy();
    expect(screen.getByText("/review")).toBeTruthy();
    // A single host isn't named.
    expect(screen.queryByText(/on a-machine/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(screen.queryByText("Your setup is ready")).toBeNull());
    expect(window.localStorage.getItem("omnigent:imports-reviewed:a")).not.toBeNull();

    cleanup();
    renderWithClient(<ImportReviewGate />);
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalled());
    expect(screen.queryByText("Your setup is ready")).toBeNull();
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
    await waitFor(() => expect(screen.queryByText("Your setup is ready")).toBeNull());
    expect(window.localStorage.getItem("omnigent:imports-reviewed:fresh")).not.toBeNull();
    expect(window.localStorage.getItem("omnigent:imports-reviewed:empty")).toBeNull();
  });

  it("stays closed when the import_review feature is disabled", () => {
    serve([host("a")], { a: ["review"] });
    renderWithClient(<ImportReviewGate />, { ...FALLBACK_SERVER_INFO, features: {} });

    // Flag off: no modal auto-opens, and the gate never even probes for hosts.
    expect(screen.queryByText("Your imports are ready")).toBeNull();
    expect(authenticatedFetchMock).not.toHaveBeenCalled();
  });
});

describe("ImportReviewGate with a requested host", () => {
  it("waits for an offline target without showing another host's imports", async () => {
    serve([host("target", { status: "offline" }), host("other")], { target: ["t"], other: ["o"] });
    requestImportReview({ hostId: "target", runner: "remote" });
    renderWithClient(<ImportReviewGate />);

    // A listed host is named rather than described by its runner.
    expect(await screen.findByText("Connecting to target-machine…")).toBeTruthy();
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalled());
    expect(screen.queryByText("/o")).toBeNull();
    expect(screen.queryByText(/is offline/)).toBeNull();
    expect(skillRequestsFor("other")).toHaveLength(0);
  });

  it("shows the target's imports once it connects", async () => {
    // Not registered yet (Arca's tunnel lags its daemon), then online.
    serve([host("other")], { target: ["t"], other: ["o"] });
    requestImportReview({ hostId: "target", runner: "remote" });
    const { client } = renderWithClient(<ImportReviewGate />);
    expect(await screen.findByText("Connecting to Arca…")).toBeTruthy();

    serve([host("other"), host("target")], { target: ["t"], other: ["o"] });
    await client.invalidateQueries({ queryKey: ["hosts"] });

    expect(await screen.findByText("/t")).toBeTruthy();
    expect(screen.getByText(/Found in your harnesses on target-machine\./)).toBeTruthy();
    expect(screen.queryByText("/o")).toBeNull();
  });

  it("treats 409 as still connecting, not an error", async () => {
    serve([host("target")], { target: ["t"] }, { absentSkillCalls: 1 });
    requestImportReview({ hostId: "target" });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText("Checking your harnesses…")).toBeTruthy();
    await waitFor(() => expect(skillRequestsFor("target")).toHaveLength(1));
    expect(screen.queryByText(/Couldn't read/)).toBeNull();
    expect(await screen.findByText("/t", {}, { timeout: 5_000 })).toBeTruthy();
    expect(screen.queryByText(/Couldn't read/)).toBeNull();
  });

  it.each([
    ["local", "Connecting this Mac…"],
    [undefined, "Connecting…"],
  ] as const)("describes an unlisted %s runner while it connects", async (runner, copy) => {
    serve([host("other")], { other: ["o"] });
    requestImportReview({ hostId: "target", runner });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText(copy)).toBeTruthy();
    await waitFor(() => expect(authenticatedFetchMock).toHaveBeenCalled());
    expect(screen.queryByText("/o")).toBeNull();
  });

  it("names a single targeted host", async () => {
    serve([host("target")], { target: ["t"] });
    requestImportReview({ hostId: "target" });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText(/Found in your harnesses on target-machine\./)).toBeTruthy();
  });

  it("shows a reviewed target and clears the request on dismiss", async () => {
    window.localStorage.setItem("omnigent:imports-reviewed:target", "x");
    serve([host("target"), host("other")], { target: ["t"], other: ["o"] });
    requestImportReview({ hostId: "target" });
    renderWithClient(<ImportReviewGate />);

    expect(await screen.findByText("/t")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    // With the request cleared, the gate falls back to unreviewed hosts.
    expect(await screen.findByText("/o")).toBeTruthy();
  });
});
