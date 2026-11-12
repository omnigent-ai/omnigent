import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/sessionsApi";
import { retryConversationLoad } from "@/store/chatStore";
import type * as ChatStoreModule from "@/store/chatStore";
import { ConversationLoadError } from "./ChatPage";

vi.mock("@/store/chatStore", async (importOriginal) => ({
  ...(await importOriginal<typeof ChatStoreModule>()),
  retryConversationLoad: vi.fn(),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderError(error: Error) {
  render(
    <MemoryRouter>
      <ConversationLoadError conversationId="conv_1" error={error} />
    </MemoryRouter>,
  );
}

describe("ConversationLoadError", () => {
  it("says a missing session wasn't found, with no retry", () => {
    renderError(new ApiError("Conversation not found", 404, "not_found"));

    expect(screen.getByRole("heading", { name: "Conversation not found" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(screen.getByRole("button", { name: "Start a new chat" })).toBeInTheDocument();
  });

  it.each([
    ["a server error", new ApiError("server restarting", 503, null)],
    ["a dropped connection", new TypeError("Failed to fetch")],
  ])("offers to load the session again after %s", (_case, error) => {
    renderError(error);

    expect(
      screen.getByRole("heading", { name: "Couldn't load this conversation" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retryConversationLoad).toHaveBeenCalledWith("conv_1");
  });
});
