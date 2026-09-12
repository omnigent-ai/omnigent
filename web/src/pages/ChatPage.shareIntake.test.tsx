import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { peekPendingShareText } from "@/lib/shareIntake";
import { TooltipProvider } from "@/components/ui/tooltip";

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

const STORAGE_KEY = "omnigent:pendingShareText";

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

/** Queues a share the way useShareIntake would, wrapped so React flushes the resulting effect. */
function queueShare(text: string): void {
  act(() => {
    useChatStore.getState().setPendingComposerText(text);
  });
}

// These tests exercise the COMPOSER's reaction to an already-queued
// pendingComposerText, seeded directly on the store -- the conversationId
// gate that decides WHEN to queue it lives in useShareIntake and is covered
// separately in shareIntake.test.ts. This mirrors how the existing
// pendingComposerAttachments drain is tested (ChatPage.mention.test.tsx),
// which also seeds the queue directly rather than going through its
// producer.
describe("Composer share-text intake", () => {
  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [],
      pendingComposerText: null,
    });
    window.sessionStorage.removeItem(STORAGE_KEY);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.sessionStorage.removeItem(STORAGE_KEY);
  });

  it("inserts shared text into an empty composer and focuses it", () => {
    window.sessionStorage.setItem(STORAGE_KEY, "hello from share");
    renderWithTooltips(<Composer {...composerProps()} />);

    queueShare("hello from share");

    expect(textarea().value).toBe("hello from share");
    expect(document.activeElement).toBe(textarea());
    // The store flag is transient and always cleared by the drain effect;
    // what matters is the DURABLE copy, consumed exactly because it was
    // actually inserted.
    expect(peekPendingShareText()).toBeNull();
    expect(useChatStore.getState().pendingComposerText).toBeNull();
  });

  it("does not autosend the inserted text", () => {
    const onSend = vi.fn();
    window.sessionStorage.setItem(STORAGE_KEY, "hello from share");
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);

    queueShare("hello from share");

    expect(onSend).not.toHaveBeenCalled();
  });

  it("offers a recoverable banner instead of silently discarding when the draft is non-empty", () => {
    window.sessionStorage.setItem(STORAGE_KEY, "hello from share");
    renderWithTooltips(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "already typing something" } });

    queueShare("hello from share");

    // The existing draft is untouched...
    expect(textarea().value).toBe("already typing something");
    // ...and the share is neither lost nor silently applied: it's offered.
    expect(screen.getByText(/hello from share/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Insert" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    // Still durably queued -- "Dismiss" is the only thing allowed to drop it.
    expect(peekPendingShareText()).toBe("hello from share");
  });

  it("banner Insert appends the shared text to the existing draft", () => {
    window.sessionStorage.setItem(STORAGE_KEY, "hello from share");
    renderWithTooltips(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "already typing something" } });
    queueShare("hello from share");

    fireEvent.click(screen.getByRole("button", { name: "Insert" }));

    expect(textarea().value).toBe("already typing something\nhello from share");
    expect(document.activeElement).toBe(textarea());
    expect(screen.queryByRole("button", { name: "Insert" })).not.toBeInTheDocument();
    expect(peekPendingShareText()).toBeNull();
  });

  it("banner Insert on a still-empty draft behaves like a plain insert", () => {
    // The draft can go from non-empty back to empty (user deletes their
    // text) before deciding on the banner -- Insert should still work,
    // not append onto nothing with a stray leading newline.
    window.sessionStorage.setItem(STORAGE_KEY, "hello from share");
    renderWithTooltips(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "typing" } });
    queueShare("hello from share");
    fireEvent.change(textarea(), { target: { value: "" } });

    fireEvent.click(screen.getByRole("button", { name: "Insert" }));

    expect(textarea().value).toBe("hello from share");
  });

  it("banner Dismiss is an explicit discard: clears the durable copy, leaves the draft alone", () => {
    window.sessionStorage.setItem(STORAGE_KEY, "hello from share");
    renderWithTooltips(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "already typing something" } });
    queueShare("hello from share");

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(textarea().value).toBe("already typing something");
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
    expect(peekPendingShareText()).toBeNull();
  });

  it("does not resurface a banner for text that was already dismissed", () => {
    window.sessionStorage.setItem(STORAGE_KEY, "hello from share");
    renderWithTooltips(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "already typing something" } });
    queueShare("hello from share");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    // A stray re-set of the same already-cleared store value must not
    // reopen the banner from nothing.
    useChatStore.setState({ pendingComposerText: null });

    expect(screen.queryByText(/hello from share/)).not.toBeInTheDocument();
  });
});
