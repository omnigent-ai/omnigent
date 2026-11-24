import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversationPage } from "@/test/sidebarMockHelpers";
import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    useProjects: () => ({
      data: [
        { id: "p_busy", name: "Busy" },
        { id: "p_quiet", name: "Quiet" },
      ],
    }),
  };
});
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
vi.mock("@/projectHome/projectTicketsSeen", () => ({
  useNewTicketCount: (projectId: string | null) => (projectId === "p_busy" ? 3 : 0),
}));

import { useConversations } from "@/hooks/useConversations";

afterEach(cleanup);

it("badges a project folder with the tickets added since its board was last opened", () => {
  const page = conversationPage([]);
  vi.mocked(useConversations).mockImplementation(() => page);
  renderSidebar({ route: "/" });
  const busy = screen.getByRole("button", { name: /^Busy/ });
  expect(within(busy).getByTestId("project-new-tickets")).toHaveAccessibleName("3 new tickets");
  const quiet = screen.getByRole("button", { name: /^Quiet/ });
  expect(within(quiet).queryByTestId("project-new-tickets")).toBeNull();
});
