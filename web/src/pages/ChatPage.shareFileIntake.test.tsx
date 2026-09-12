import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { TooltipProvider } from "@/components/ui/tooltip";
import { clearSessionDrafts } from "@/lib/sessionDrafts";

// Composer reads workspace files via a TanStack query hook (for "@"-file
// mentions); these tests don't exercise that.
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof UseWorkspaceChangedFilesModule>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({ data: undefined }),
    useWorkspaceDirectory: () => ({ data: undefined }),
  };
});
vi.mock("@/hooks/useGithub", () => ({
  useGithubInfo: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({ session: { hostId: null }, isLoading: false, error: null }),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHostsModule>()),
  useHosts: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthProviderModule>()),
  useSessionHostOnline: () => undefined,
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({}),
}));

import { Composer } from "./ChatPage";

function composerProps(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return {
    status: "idle" as const,
    isWorking: false,
    disabled: false,
    onSend: vi.fn(),
    onStop: vi.fn(),
    agents: undefined,
    selectedAgentId: null,
    permissionLevel: null,
    readOnlyReason: null,
    replyQuotes: [],
    onRemoveQuote: vi.fn(),
    onClearAllQuotes: vi.fn(),
    effortLevels: ["low", "medium", "high"] as const,
    showEffort: true,
    showModels: false,
    modelPickerKind: null,
    codexModelOptions: [],
    showCodexPlanMode: false,
    ...overrides,
  };
}

function textarea() {
  return screen.getByLabelText("Message the agent") as HTMLTextAreaElement;
}

function renderWithTooltips(ui: ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>);
}

/** Queues shared files the way useSharedFileIntake would, flushed via act(). */
function queueSharedFiles(files: File[]): void {
  act(() => {
    useChatStore.getState().setPendingComposerFiles(files);
  });
}

// These tests exercise the COMPOSER's reaction to already-decoded
// pendingComposerFiles, seeded directly on the store -- the
// conversationId/live-bridge gate that decides WHEN to queue it lives in
// useSharedFileIntake and is covered separately in shareFileIntake.test.ts.
// Mirrors the established split for share TEXT (ChatPage.shareIntake.test.tsx
// / shareIntake.test.ts) and for "@"-mention attachments
// (ChatPage.mention.test.tsx), which also seed the queue directly.
describe("Composer share-file intake", () => {
  beforeEach(() => {
    clearSessionDrafts();
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [],
      pendingComposerFiles: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    clearSessionDrafts();
  });

  it("attaches a shared file through the existing upload flow", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    const file = new File([new Uint8Array(10)], "shot.png", { type: "image/png" });

    queueSharedFiles([file]);

    expect(screen.getByText("shot.png")).toBeInTheDocument();
    expect(useChatStore.getState().pendingComposerFiles).toBeNull();
  });

  it("does not autosend an attached shared file", () => {
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    const file = new File([new Uint8Array(10)], "shot.png", { type: "image/png" });

    queueSharedFiles([file]);

    expect(onSend).not.toHaveBeenCalled();
  });

  it("rejects an unsupported shared file type the same way a manual attach would", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    const bad = new File([new Uint8Array(10)], "clip.mp4", { type: "video/mp4" });

    queueSharedFiles([bad]);

    expect(screen.queryByText("clip.mp4")).not.toBeInTheDocument();
    expect(screen.getByText(/can't be attached/)).toBeInTheDocument();
    expect(useChatStore.getState().pendingComposerFiles).toBeNull();
  });

  it("cancellation: a shared attachment can be removed like any other", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    const file = new File([new Uint8Array(10)], "shot.png", { type: "image/png" });
    queueSharedFiles([file]);
    expect(screen.getByText("shot.png")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Remove shot.png" }));

    expect(screen.queryByText("shot.png")).not.toBeInTheDocument();
  });

  it("appends to an existing draft/attachment rather than replacing it", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "look at this" } });
    const existing = new File([new Uint8Array(10)], "first.png", { type: "image/png" });
    queueSharedFiles([existing]);

    const shared = new File([new Uint8Array(10)], "shared.png", { type: "image/png" });
    queueSharedFiles([shared]);

    expect(textarea().value).toBe("look at this");
    expect(screen.getByText("first.png")).toBeInTheDocument();
    expect(screen.getByText("shared.png")).toBeInTheDocument();
  });
});
