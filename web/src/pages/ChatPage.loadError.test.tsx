import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type * as NewChatDialogModule from "@/shell/NewChatDialog";
import type * as ChatStoreModule from "@/store/chatStore";
import type { PendingInitialPrompt } from "@/store/chatStore";

// Control the handoff seams the load-error screen depends on: the landing
// composer's accept/refuse decision and the failed-send draft lookup. Everything
// else in NewChatDialog / chatStore stays real.
const mocks = vi.hoisted(() => ({
  restoreLandingDraftMessage: vi.fn(),
  peekFailedSendDraft: vi.fn(),
  clearFailedSendDraft: vi.fn(),
}));

vi.mock("@/shell/NewChatDialog", async (importOriginal) => ({
  ...(await importOriginal<typeof NewChatDialogModule>()),
  restoreLandingDraftMessage: mocks.restoreLandingDraftMessage,
}));

vi.mock("@/store/chatStore", async (importOriginal) => ({
  ...(await importOriginal<typeof ChatStoreModule>()),
  peekFailedSendDraft: mocks.peekFailedSendDraft,
  clearFailedSendDraft: mocks.clearFailedSendDraft,
}));

import { ConversationLoadError } from "./ChatPage";

afterEach(cleanup);

const onStrandedPromptRetired = vi.fn();

function renderError(strandedPrompt: PendingInitialPrompt | null) {
  return render(
    <MemoryRouter>
      <ConversationLoadError
        conversationId="conv_abc"
        error={new Error("boom")}
        strandedPrompt={strandedPrompt}
        onStrandedPromptRetired={onStrandedPromptRetired}
      />
    </MemoryRouter>,
  );
}

function clickStartNewChat() {
  fireEvent.click(screen.getByRole("button", { name: /start a new chat/i }));
}

describe("ConversationLoadError stranded-prompt handoff", () => {
  beforeEach(() => {
    mocks.restoreLandingDraftMessage.mockReset();
    mocks.peekFailedSendDraft.mockReset().mockReturnValue(null);
    mocks.clearFailedSendDraft.mockReset();
    onStrandedPromptRetired.mockReset();
  });

  it("retires the cached prompt once the landing composer accepts the stranded text", () => {
    mocks.restoreLandingDraftMessage.mockReturnValue(true);

    renderError({ text: "read the README", skill: null, files: [] });
    clickStartNewChat();

    expect(mocks.restoreLandingDraftMessage).toHaveBeenCalledWith("read the README", []);
    // The source now lives in the landing composer, so the ChatPage cache is
    // retired via the callback and no failed-send draft needs clearing.
    expect(onStrandedPromptRetired).toHaveBeenCalledTimes(1);
    expect(mocks.clearFailedSendDraft).not.toHaveBeenCalled();
  });

  it("preserves the stranded prompt when a newer landing draft refuses the restore", () => {
    mocks.restoreLandingDraftMessage.mockReturnValue(false);

    renderError({ text: "read the README", skill: null, files: [] });
    clickStartNewChat();

    expect(mocks.restoreLandingDraftMessage).toHaveBeenCalledOnce();
    // A refused transfer must leave the source intact, so neither the cache nor
    // the failed-send draft is retired.
    expect(onStrandedPromptRetired).not.toHaveBeenCalled();
    expect(mocks.clearFailedSendDraft).not.toHaveBeenCalled();
  });

  it("clears the failed-send draft and retires any stranded cache when a failed send exists", () => {
    const file = new File(["x"], "shot.png", { type: "image/png" });
    mocks.peekFailedSendDraft.mockReturnValue({ text: "half-typed", files: [file] });
    mocks.restoreLandingDraftMessage.mockReturnValue(true);

    // A settled failed send supplies the restored text, but the still-cached
    // initial prompt must also be retired or a browser-back re-dispatches it.
    renderError({ text: "initial", skill: null, files: [] });
    clickStartNewChat();

    expect(mocks.restoreLandingDraftMessage).toHaveBeenCalledWith("half-typed", [file]);
    expect(mocks.clearFailedSendDraft).toHaveBeenCalledWith("conv_abc");
    expect(onStrandedPromptRetired).toHaveBeenCalledTimes(1);
  });

  it("clears only the failed-send draft when there is no cached initial prompt", () => {
    const file = new File(["x"], "shot.png", { type: "image/png" });
    mocks.peekFailedSendDraft.mockReturnValue({ text: "half-typed", files: [file] });
    mocks.restoreLandingDraftMessage.mockReturnValue(true);

    renderError(null);
    clickStartNewChat();

    expect(mocks.restoreLandingDraftMessage).toHaveBeenCalledWith("half-typed", [file]);
    expect(mocks.clearFailedSendDraft).toHaveBeenCalledWith("conv_abc");
    expect(onStrandedPromptRetired).not.toHaveBeenCalled();
  });

  it("retires nothing when there is no stranded prompt and no failed draft", () => {
    renderError(null);
    clickStartNewChat();

    expect(mocks.restoreLandingDraftMessage).not.toHaveBeenCalled();
    expect(onStrandedPromptRetired).not.toHaveBeenCalled();
    expect(mocks.clearFailedSendDraft).not.toHaveBeenCalled();
  });
});
