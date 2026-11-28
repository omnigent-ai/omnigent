import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { conversation, conversationPage } from "@/test/sidebarMockHelpers";

const renders = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  const projects = { data: [] };
  return { ...conversationHooksMock(), useProjects: () => projects };
});
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
vi.mock("./useSessionActions", async (importOriginal) => {
  const original = await importOriginal<typeof SessionActionsModule>();
  return {
    ...original,
    useSessionActions: (options: Parameters<typeof original.useSessionActions>[0]) => {
      renders(options.conversation.id);
      return original.useSessionActions(options);
    },
  };
});

import type * as SessionActionsModule from "./useSessionActions";
import { useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

afterEach(() => {
  cleanup();
  localStorage.clear();
  renders.mockClear();
});

it("does not render unchanged inactive rows when the open session changes", async () => {
  const rows = ["first", "second", "inactive"].map((id) => conversation(id));
  vi.mocked(useConversations).mockReturnValue(conversationPage(rows));
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  client.setQueryData(["hosts", { includeSandbox: true, sourceHostId: undefined }], []);
  for (const row of rows) {
    client.setQueryData(["session", row.id], { id: row.id, parentSessionId: null });
  }
  function SwitchSession() {
    const navigate = useNavigate();
    return (
      <button type="button" onClick={() => navigate("/c/second")}>
        Switch session
      </button>
    );
  }
  render(
    <QueryClientProvider client={client}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/first"]}>
            <SwitchSession />
            <Routes>
              <Route path="/c/:conversationId" element={<Sidebar open onClose={vi.fn()} />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
  await waitFor(() =>
    expect(screen.getByRole("link", { name: /^first/ })).toHaveClass("bg-[var(--sidebar-active)]"),
  );
  expect(renders.mock.calls.flat()).toContain("inactive");
  renders.mockClear();

  fireEvent.click(screen.getByRole("button", { name: "Switch session" }));

  await waitFor(() =>
    expect(screen.getByRole("link", { name: /^second/ })).toHaveClass("bg-[var(--sidebar-active)]"),
  );
  expect(screen.getByRole("link", { name: /^first/ })).not.toHaveClass(
    "bg-[var(--sidebar-active)]",
  );
  expect(renders.mock.calls.flat()).toContain("first");
  expect(renders.mock.calls.flat()).toContain("second");
  expect(renders.mock.calls.flat()).not.toContain("inactive");
  expect(renders).toHaveBeenCalledTimes(2);
});
