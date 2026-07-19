import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RevertSessionDialog } from "./RevertSessionDialog";
import { fetchSessionItemsPage, revertSession } from "@/lib/sessionsApi";
import { setSessionTextDraft } from "@/lib/sessionDrafts";

const { reloadCurrentMock } = vi.hoisted(() => ({ reloadCurrentMock: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({
  fetchSessionItemsPage: vi.fn(),
  revertSession: vi.fn(),
}));
vi.mock("@/lib/sessionDrafts", () => ({ setSessionTextDraft: vi.fn() }));
vi.mock("@/store/chatStore", () => ({
  useChatStore: { getState: () => ({ reloadCurrent: reloadCurrentMock }) },
}));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const fetchItemsMock = vi.mocked(fetchSessionItemsPage);
const revertMock = vi.mocked(revertSession);
const setDraftMock = vi.mocked(setSessionTextDraft);

function renderDialog(initialUserMessageId: string | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RevertSessionDialog
          sourceSessionId="conv_source"
          sourceWorkspace="/repo"
          canModifySource
          initialUserMessageId={initialUserMessageId}
          open
          onOpenChange={vi.fn()}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  reloadCurrentMock.mockReset();
  reloadCurrentMock.mockResolvedValue(undefined);
  fetchItemsMock.mockReset();
  revertMock.mockReset();
  setDraftMock.mockReset();
  fetchItemsMock.mockResolvedValue({
    items: [
      {
        id: "msg_1",
        type: "message",
        response_id: "resp_1",
        status: "completed",
        role: "user",
        content: [{ type: "input_text", text: "first prompt" }],
      },
      {
        id: "msg_2",
        type: "message",
        response_id: "resp_2",
        status: "completed",
        role: "user",
        content: [{ type: "input_text", text: "second prompt" }],
      },
    ],
    hasMore: false,
  });
  revertMock.mockResolvedValue({
    session: { id: "conv_source" } as Awaited<ReturnType<typeof revertSession>>["session"],
    draft: "first prompt",
    filesReverted: true,
    fileRevertError: null,
  });
});

afterEach(cleanup);

describe("RevertSessionDialog", () => {
  it("reloads the same session before restoring the selected prompt draft", async () => {
    let finishReload = (): void => {};
    reloadCurrentMock.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        finishReload = resolve;
      }),
    );
    renderDialog("msg_1");

    await screen.findByText("first prompt");
    expect(screen.getByDisplayValue("msg_1")).toBeChecked();
    fireEvent.click(screen.getByText("Restore file changes"));
    fireEvent.click(screen.getByRole("button", { name: "Revert" }));

    await waitFor(() =>
      expect(revertMock).toHaveBeenCalledWith("conv_source", "msg_1", {
        revertFiles: true,
      }),
    );
    await waitFor(() => expect(reloadCurrentMock).toHaveBeenCalledOnce());
    expect(setDraftMock).not.toHaveBeenCalled();

    finishReload();
    await waitFor(() => expect(setDraftMock).toHaveBeenCalledWith("conv_source", "first prompt"));
  });
});
