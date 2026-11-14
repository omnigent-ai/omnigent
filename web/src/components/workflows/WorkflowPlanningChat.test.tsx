import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { useCanEdit } from "@/hooks/usePermissions";
import { useWorkflow, type WorkflowDetail } from "@/hooks/useWorkflow";
import { authenticatedFetch } from "@/lib/identity";
import type * as IdentityModule from "@/lib/identity";
import type { Session } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";
import { WorkflowPlanningChat } from "./WorkflowPlanningChat";

// Session launch and transcript behavior are covered by their own component tests.
vi.mock("@/components/workflows/WorkflowSessionDialog", () => ({
  WorkflowSessionForm: ({
    onStarted,
  }: {
    onStarted: (session: Session, prompt: string) => Promise<void>;
  }) => (
    <button
      type="button"
      onClick={() => void onStarted({ id: SESSION } as Session, "Draft the release plan")}
    >
      Start planning
    </button>
  ),
}));
vi.mock("@/components/chat/SideChatPane", () => ({
  SideChatPane: ({
    childId,
    fullSession,
    readOnly,
  }: {
    childId: string;
    fullSession: boolean;
    readOnly: boolean;
  }) => (
    <div
      data-testid="chat"
      data-session={childId}
      data-full={String(fullSession)}
      data-read-only={String(readOnly)}
    />
  ),
}));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn() }));
vi.mock("@/hooks/useSession", () => ({
  useSession: (id: string | null) => ({
    session: id ? { id, labels: {}, archived: false } : null,
    error: null,
  }),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));

const SESSION = "a".repeat(32);
const WORKFLOW: WorkflowDetail = {
  id: "b".repeat(32),
  name: "Release",
  description: "Prepare a release",
  steps: [],
  is_draft: true,
};

function Editor() {
  const { data } = useWorkflow(WORKFLOW.id);
  return data && <WorkflowPlanningChat workflow={data} onClose={vi.fn()} />;
}

function openEditor() {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter>
        <Editor />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(useCanEdit).mockReturnValue(true);
  useChatStore.getState().clearSideChatDraft(SESSION);
});
afterEach(() => {
  cleanup();
  useChatStore.getState().clearSideChatDraft(SESSION);
  useChatStore.getState().clearSideChatComposer(SESSION);
});

it("attaches the real session before seeding its first message, and restores it after reopening", async () => {
  let stored = WORKFLOW;
  vi.mocked(authenticatedFetch).mockImplementation(async (url, options) => {
    expect(url).toBe(`/v1/workflows/${WORKFLOW.id}`);
    if (options?.method === "PATCH") {
      expect(useChatStore.getState().sideChatDrafts[SESSION]).toBeUndefined();
      expect(JSON.parse(String(options.body))).toEqual({
        planning_session_id: SESSION,
      });
      stored = { ...stored, planning_session_id: SESSION };
    }
    return new Response(JSON.stringify(stored));
  });
  openEditor();
  fireEvent.click(await screen.findByRole("button", { name: "Start planning" }));
  const chat = await screen.findByTestId("chat");
  expect(chat).toHaveAttribute("data-session", SESSION);
  expect(chat).toHaveAttribute("data-full", "true");
  expect(chat).toHaveAttribute("data-read-only", "false");
  expect(useChatStore.getState().sideChatDrafts[SESSION]).toBe("Draft the release plan");
  expect(screen.getByRole("link", { name: "Open planning session in full" })).toHaveAttribute(
    "href",
    `/c/${SESSION}`,
  );

  cleanup();
  useChatStore.getState().clearSideChatDraft(SESSION);
  openEditor();
  expect(await screen.findByTestId("chat")).toHaveAttribute("data-session", SESSION);
  expect(screen.queryByRole("button", { name: "Start planning" })).toBeNull();
  expect(useChatStore.getState().sideChatDrafts[SESSION]).toBeUndefined();
});

it("respects the session's edit permission when reopening", async () => {
  vi.mocked(useCanEdit).mockReturnValue(false);
  vi.mocked(authenticatedFetch).mockImplementation(
    async () => new Response(JSON.stringify({ ...WORKFLOW, planning_session_id: SESSION })),
  );
  openEditor();
  expect(await screen.findByTestId("chat")).toHaveAttribute("data-read-only", "true");
});
