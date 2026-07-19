import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RevertSessionDialog } from "./RevertSessionDialog";
import type { MessageItem } from "@/lib/conversationItems";
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

function userMessage(id: string, text: string): MessageItem {
  return {
    id,
    type: "message",
    response_id: `resp_${id}`,
    status: "completed",
    role: "user",
    content: [{ type: "input_text", text }],
  };
}

function renderDialog(initialUserMessageId: string | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const dialog = (open: boolean) => (
    <QueryClientProvider client={client}>
      <RevertSessionDialog
        sourceSessionId="conv_source"
        sourceWorkspace="/repo"
        canModifySource
        initialUserMessageId={initialUserMessageId}
        open={open}
        onOpenChange={vi.fn()}
      />
    </QueryClientProvider>
  );
  const view = render(dialog(true));
  return { ...view, setOpen: (open: boolean) => view.rerender(dialog(open)) };
}

beforeEach(() => {
  reloadCurrentMock.mockReset();
  reloadCurrentMock.mockResolvedValue(undefined);
  fetchItemsMock.mockReset();
  revertMock.mockReset();
  setDraftMock.mockReset();
  fetchItemsMock.mockResolvedValue({
    items: [userMessage("msg_1", "first prompt"), userMessage("msg_2", "second prompt")],
    hasMore: false,
  });
  revertMock.mockResolvedValue({ draft: "second prompt", fileRevertError: null });
});

afterEach(cleanup);

describe("RevertSessionDialog", () => {
  it("restores the draft and reloads current messages when reopened", async () => {
    let finishReload!: () => void;
    reloadCurrentMock.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishReload = resolve;
        }),
    );
    const { setOpen } = renderDialog("msg_2");

    await screen.findByText("second prompt");
    expect(screen.getByDisplayValue("msg_2")).toBeChecked();
    fireEvent.click(screen.getByText("Revert file changes"));
    fireEvent.click(screen.getByRole("button", { name: "Revert" }));

    await waitFor(() => expect(revertMock).toHaveBeenCalledWith("conv_source", "msg_2", true));
    await waitFor(() => expect(reloadCurrentMock).toHaveBeenCalledOnce());
    expect(setDraftMock).not.toHaveBeenCalled();

    finishReload();
    await waitFor(() => expect(setDraftMock).toHaveBeenCalledWith("conv_source", "second prompt"));

    setOpen(false);
    fetchItemsMock.mockResolvedValueOnce({
      items: [userMessage("msg_1", "first prompt")],
      hasMore: false,
    });
    setOpen(true);

    await screen.findByText("first prompt");
    expect(fetchItemsMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("second prompt")).not.toBeInTheDocument();
  });
});
