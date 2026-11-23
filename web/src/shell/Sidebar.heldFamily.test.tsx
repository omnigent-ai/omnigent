// A collapsed project folder holds the open session's family so nested agents stay
// reachable. Drives the real Sidebar and its data hooks against a fetch stub.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { LEVEL_OWNER } from "@/lib/permissionsApi";
import { Sidebar } from "./Sidebar";
import { COLLAPSED_SESSION_FAMILIES_STORAGE_KEY } from "./sidebarNav";

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

const PROJECT = { id: "proj_oncall", name: "Oncall" };
const FAMILY = { id: "fam_coordinator", representative_id: "conv_coordinator", pinned: false };

function row(id: string, updatedAt: number, extra: Record<string, unknown> = {}) {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: updatedAt,
    updated_at: updatedAt,
    labels: {},
    permission_level: LEVEL_OWNER,
    status: "idle",
    project_id: PROJECT.id,
    ...extra,
  };
}

function projectSessions(nestedB: Record<string, unknown> = {}) {
  return [
    row("conv_standalone", 400),
    row("conv_coordinator", 300, { family: FAMILY }),
    row("conv_nested_a", 200, { family: FAMILY }),
    row("conv_nested_b", 100, { family: FAMILY, ...nestedB }),
  ];
}

let sessions = projectSessions();

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
  const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const url = new URL(href, "http://localhost");
  if (url.pathname === "/v1/sessions/projects") return json([PROJECT]);
  if (url.pathname === "/v1/sessions") {
    const pinned = url.searchParams.get("pinned") === "true";
    return json({ data: pinned ? [] : sessions, first_id: null, last_id: null, has_more: false });
  }
  const session = sessions.find((item) => url.pathname === `/v1/sessions/${item.id}`);
  if (session) return json(session);
  return json({ data: [] });
});

function setViewport(mobile: boolean) {
  const base = window.matchMedia("(max-width: 1180.98px)");
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    ...base,
    media: query,
    matches: mobile && query === "(max-width: 1180.98px)",
  }));
}

async function openAt(sessionId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const sidebar = <Sidebar open onClose={vi.fn()} />;
  render(
    <QueryClientProvider client={queryClient}>
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, sibling_sessions: true }}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={[`/c/${sessionId}`]}>
              <Routes>
                <Route path="/c/:conversationId" element={sidebar} />
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  await screen.findByRole("button", { name: /^Oncall/ });
  // The open session's row arrives with the rest of its family in one list page.
  await waitFor(() => expect(listedIds()).toContain(sessionId));
}

function folder() {
  return screen.getByRole("button", { name: /^Oncall/ }).closest("section")!;
}

function listedIds() {
  return [...folder().querySelectorAll("li[data-sidebar-session-id]")].map((li) =>
    li.getAttribute("data-sidebar-session-id"),
  );
}

beforeEach(() => {
  localStorage.clear();
  sessions = projectSessions();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("a collapsed project folder", () => {
  it.each([
    ["phone", true],
    ["desktop", false],
  ])("lists the open coordinator's nested agents without opening (%s)", async (_layout, mobile) => {
    setViewport(mobile);
    await openAt("conv_coordinator");

    expect(screen.getByRole("button", { name: /^Oncall/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(listedIds()).toEqual(["conv_coordinator", "conv_nested_a", "conv_nested_b"]);
    expect(
      within(folder()).getByRole("button", { name: "Collapse session family" }),
    ).toBeInTheDocument();
  });

  // The remaining cases exercise shared family logic, so they run at the reported phone width.
  it("lists the family around an open nested agent", async () => {
    setViewport(true);
    await openAt("conv_nested_b");

    expect(listedIds()).toEqual(["conv_coordinator", "conv_nested_a", "conv_nested_b"]);
  });

  it("keeps a family the user collapsed collapsed, with its expand control", async () => {
    setViewport(true);
    localStorage.setItem(COLLAPSED_SESSION_FAMILIES_STORAGE_KEY, JSON.stringify([FAMILY.id]));
    await openAt("conv_coordinator");

    expect(listedIds()).toEqual(["conv_coordinator"]);
    fireEvent.click(within(folder()).getByRole("button", { name: "Expand session family" }));
    expect(listedIds()).toEqual(["conv_coordinator", "conv_nested_a", "conv_nested_b"]);
    expect(screen.getByRole("button", { name: /^Oncall/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("still lists an open nested agent whose family the user collapsed", async () => {
    setViewport(true);
    localStorage.setItem(COLLAPSED_SESSION_FAMILIES_STORAGE_KEY, JSON.stringify([FAMILY.id]));
    await openAt("conv_nested_a");

    expect(listedIds()).toEqual(["conv_nested_a"]);
  });

  it("badges the folder header for a nested agent its collapsed family hides", async () => {
    setViewport(true);
    localStorage.setItem(COLLAPSED_SESSION_FAMILIES_STORAGE_KEY, JSON.stringify([FAMILY.id]));
    sessions = projectSessions({ pending_elicitations_count: 1 });
    await openAt("conv_coordinator");

    const header = screen.getByRole("button", { name: /^Oncall/ });
    expect(within(header).getByText("Needs response")).toBeInTheDocument();
    fireEvent.click(within(folder()).getByRole("button", { name: "Expand session family" }));
    expect(within(header).queryByText("Needs response")).toBeNull();
    expect(within(folder()).getAllByText("Needs response")).toHaveLength(1);
  });
});
