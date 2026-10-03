import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type FilesystemAttachmentPolicy } from "@/lib/capabilities";
import { createInitialConversationState } from "@/store/conversationState";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SideChatPane } from "./SideChatPane";

const send = vi.hoisted(() => vi.fn(async () => {}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: Object.assign(
    (selector: (state: unknown) => unknown) => selector({ send, clearSideChatDraft: () => {} }),
    {
      getState: () => ({ sideChatDrafts: {} }),
    },
  ),
  ensureConversationStreamed: async () => {},
}));
vi.mock("@/hooks/useConversationEntryState", () => ({
  useConversationEntryState: () => ({ ...createInitialConversationState(), boundAgentId: "agent" }),
}));
vi.mock("@/components/ComposerMicButton", () => ({ ComposerMicButton: () => null }));
vi.mock("@/components/chat/chatBubbleParts", () => ({
  liveCandidateAssistantIndex: () => -1,
  computeIsWorking: () => false,
  computeIsTurnActive: () => false,
  reorderCommittedRequestElicitations: (bubbles: unknown) => bubbles,
  stripGatedSubagentRoutingChips: (bubbles: unknown) => bubbles,
  shouldShowWorkingIndicator: () => false,
}));

const policy: FilesystemAttachmentPolicy = {
  allowed_extensions: [".mp4"],
  denied_extensions: [".exe"],
  max_bytes: 100,
  max_files: 2,
  max_total_bytes: 200,
  harnesses: ["claude-native"],
};
function mount(value: FilesystemAttachmentPolicy | undefined) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, filesystem_attachment_policy: value }}>
        <TooltipProvider>
          <SideChatPane childId="child" />
        </TooltipProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  return document.querySelector('input[type="file"]') as HTMLInputElement;
}
afterEach(() => {
  cleanup();
  send.mockReset();
});

describe("side chat attachment policy", () => {
  it.each(["list", "wildcard", "absent"])("uses %s policy for its picker", (mode) => {
    const input = mount(
      mode === "list"
        ? policy
        : mode === "wildcard"
          ? { ...policy, allowed_extensions: "*" }
          : undefined,
    );
    if (mode === "list") expect(input.accept).toContain(".mp4");
    else expect(input.hasAttribute("accept")).toBe(false);
  });
  it("validates picks and reports rejected files", () => {
    const input = mount({ ...policy, allowed_extensions: [] });
    fireEvent.change(input, {
      target: { files: [new File(["video"], "clip.mp4", { type: "video/mp4" })] },
    });
    expect(screen.getByRole("alert").textContent).toContain("server policy");
    expect(screen.getByTestId("side-chat-send")).toBeDisabled();
  });
  it("preserves text and files and displays server rejection", async () => {
    send.mockImplementationOnce(async (...args: unknown[]) => {
      (args[3] as { onError?: (message: string) => void }).onError?.("413 server quota exceeded");
    });
    const input = mount(policy);
    fireEvent.change(input, {
      target: { files: [new File(["video"], "clip.mp4", { type: "video/mp4" })] },
    });
    fireEvent.change(screen.getByTestId("side-chat-input"), { target: { value: "analyze this" } });
    fireEvent.click(screen.getByTestId("side-chat-send"));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain("413 server quota exceeded"),
    );
    expect(screen.getByTestId("side-chat-input")).toHaveValue("analyze this");
    expect(screen.getByText("clip.mp4")).toBeInTheDocument();
  });
});

it("preserves newer draft when an older upload fails later", async () => {
  let rejectUpload: ((message: string) => void) | undefined;
  send.mockImplementationOnce(async (...args: unknown[]) => {
    rejectUpload = (args[3] as { onError?: (message: string) => void }).onError;
  });
  const input = mount(policy);
  fireEvent.change(input, {
    target: { files: [new File(["video"], "clip.mp4", { type: "video/mp4" })] },
  });
  fireEvent.change(screen.getByTestId("side-chat-input"), { target: { value: "first draft" } });
  fireEvent.click(screen.getByTestId("side-chat-send"));
  fireEvent.change(screen.getByTestId("side-chat-input"), { target: { value: "new draft" } });
  fireEvent.change(input, {
    target: { files: [new File(["next"], "next.mp4", { type: "video/mp4" })] },
  });
  await act(async () => rejectUpload?.("413 quota exceeded"));
  expect(screen.getByTestId("side-chat-input")).toHaveValue("first draft\n\nnew draft");
  expect(screen.getByText("clip.mp4")).toBeInTheDocument();
  expect(screen.getByText("next.mp4")).toBeInTheDocument();
});
