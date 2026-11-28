import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { StickToBottom } from "use-stick-to-bottom";
import type * as StickToBottomModule from "use-stick-to-bottom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type { Bubble } from "@/lib/renderItems";
import { ForkDialogContextProvider } from "@/shell/ForkDialogContext";
import {
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommandsStore,
} from "@/store/paletteCommands";

import type { UserMessageBlock } from "@/lib/blocks";
import type * as HotkeysModule from "@/lib/hotkeys";
import { writePeerSessionMessageDisplay } from "@/lib/peerSessionMessagePreferences";
import { useChatStore } from "@/store/chatStore";

import { useStopResponsePaletteCommand } from "./stopResponse";
import { Transcript } from "./Transcript";
import { NO_PROMPT_YET } from "./latestMessageCommands";
import { AT_NEWEST_MESSAGE, TranscriptPaletteCommands } from "./TranscriptPaletteCommands";

const platform = vi.hoisted(() => ({ mac: false }));
vi.mock("@/lib/hotkeys", async (importOriginal) => ({
  ...(await importOriginal<typeof HotkeysModule>()),
  isMacPlatform: () => platform.mac,
}));
const clipboard = vi.hoisted(() => ({
  text: vi.fn(async (_text: string) => {}),
  markdown: vi.fn(async (_text: string) => {}),
}));
vi.mock("@/lib/clipboard", () => ({ copyText: clipboard.text }));
vi.mock("@/lib/copyMarkdown", () => ({ copyMarkdown: clipboard.markdown }));
// Set to stand in for the transcript's scroll state; unset, the real one is used.
const scroll = vi.hoisted(() => ({
  state: null as { isAtBottom: boolean; scrollToBottom: (behavior: string) => void } | null,
}));
vi.mock("use-stick-to-bottom", async (importOriginal) => {
  const original = await importOriginal<typeof StickToBottomModule>();
  return {
    ...original,
    useStickToBottomContext: () => scroll.state ?? original.useStickToBottomContext(),
  };
});

const BUBBLES: Bubble[] = [
  { kind: "user", itemId: "u1", content: [{ type: "input_text", text: "Fix the parser" }] },
  {
    kind: "assistant",
    responseId: "r1",
    stableId: "r1",
    lifecycle: "completed",
    error: null,
    items: [{ kind: "text", text: "Done." } as Extract<Bubble, { kind: "assistant" }>["items"][0]],
  },
];

function withFlag(on: boolean, openForkDialog = vi.fn()) {
  const info = { ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: on } };
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <CapabilitiesProvider info={info}>
        <ForkDialogContextProvider value={{ canFork: true, openForkDialog }}>
          <StickToBottom>{children}</StickToBottom>
        </ForkDialogContextProvider>
      </CapabilitiesProvider>
    );
  };
}

const conversationCommands = (): PaletteCommand[] =>
  Object.entries(usePaletteCommandsStore.getState().groups)
    .filter(([source]) => source.startsWith("conversation"))
    .flatMap(([, group]) => group.commands);
const command = (label: string) => {
  const found = conversationCommands().find((candidate) => candidate.label === label);
  if (!found) throw new Error(`No ⌘K command "${label}"`);
  return found;
};

afterEach(() => {
  cleanup();
  scroll.state = null;
  vi.clearAllMocks();
  useChatStore.setState({ blocks: [], conversationId: null });
  writePeerSessionMessageDisplay("collapse");
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("TranscriptPaletteCommands", () => {
  it("offers nothing with quick_pick_palette off", () => {
    render(<TranscriptPaletteCommands conversationId="c1" bubbles={BUBBLES} />, {
      wrapper: withFlag(false),
    });
    expect(conversationCommands()).toEqual([]);
  });

  it("copies and links the latest response and prompt, as their buttons do", async () => {
    render(<TranscriptPaletteCommands conversationId="c1" bubbles={BUBBLES} />, {
      wrapper: withFlag(true),
    });
    expect(conversationCommands().map((candidate) => candidate.label)).toEqual([
      "Copy latest response",
      "Copy link to latest response",
      "Fork from latest response…",
      "Copy latest prompt",
      "Copy link to latest prompt",
      "Scroll to newest message",
    ]);
    await act(async () => command("Copy latest response").run());
    expect(clipboard.markdown).toHaveBeenCalledWith("Done.");
    await act(async () => command("Copy latest prompt").run());
    expect(clipboard.text).toHaveBeenLastCalledWith("Fix the parser");
    await act(async () => command("Copy link to latest prompt").run());
    expect(clipboard.text).toHaveBeenLastCalledWith(expect.stringContaining("message=u1"));
  });

  it("links the latest response, and copies what shows when run, not when listed", async () => {
    const { rerender } = render(
      <TranscriptPaletteCommands conversationId="c1" bubbles={BUBBLES} />,
      {
        wrapper: withFlag(true),
      },
    );
    await act(async () => command("Copy link to latest response").run());
    expect(clipboard.text).toHaveBeenLastCalledWith(expect.stringContaining("r1"));

    // New text with the same availability keeps the rows, and Copy reads the text then.
    const listed = command("Copy latest response");
    const edited = BUBBLES.map((bubble) =>
      bubble.kind === "assistant"
        ? {
            ...bubble,
            items: [{ kind: "text", text: "Done, and tested." } as (typeof bubble.items)[0]],
          }
        : bubble,
    );
    rerender(<TranscriptPaletteCommands conversationId="c1" bubbles={edited} />);
    expect(command("Copy latest response")).toBe(listed);
    await act(async () => listed.run());
    expect(clipboard.markdown).toHaveBeenLastCalledWith("Done, and tested.");

    // A newer response with the same availability keeps the link row, which links it then.
    const listedLink = command("Copy link to latest response");
    const newer = edited.map((bubble) =>
      bubble.kind === "assistant" ? { ...bubble, responseId: "r2" } : bubble,
    );
    rerender(<TranscriptPaletteCommands conversationId="c1" bubbles={newer} />);
    expect(command("Copy link to latest response")).toBe(listedLink);
    await act(async () => listedLink.run());
    expect(clipboard.text).toHaveBeenLastCalledWith(expect.stringContaining("r2"));
  });

  it("scrolls to the newest message as the transcript's arrow does, greyed once there", () => {
    const scrollToBottom = vi.fn();
    scroll.state = { isAtBottom: false, scrollToBottom };
    const { rerender } = render(
      <TranscriptPaletteCommands conversationId="c1" bubbles={BUBBLES} />,
      {
        wrapper: withFlag(true),
      },
    );
    act(() => command("Scroll to newest message").run());
    expect(scrollToBottom).toHaveBeenCalledWith("instant");

    scroll.state = { isAtBottom: true, scrollToBottom };
    rerender(<TranscriptPaletteCommands conversationId="c1" bubbles={BUBBLES} />);
    expect(command("Scroll to newest message").availability).toEqual({
      available: false,
      reason: AT_NEWEST_MESSAGE,
    });
  });

  it("forks from the latest response through the fork dialog", () => {
    const openForkDialog = vi.fn();
    render(<TranscriptPaletteCommands conversationId="c1" bubbles={BUBBLES} />, {
      wrapper: withFlag(true, openForkDialog),
    });
    act(() => command("Fork from latest response…").run());
    expect(openForkDialog).toHaveBeenCalledWith({ upToResponseId: "r1" });
  });

  it("steps through the prompts as ⌘⌥↑ and ⌘⌥↓ do, once there is one", () => {
    const messageNav = { goPrev: vi.fn(), goNext: vi.fn() };
    const { rerender } = render(
      <TranscriptPaletteCommands conversationId="c1" bubbles={BUBBLES} messageNav={messageNav} />,
      { wrapper: withFlag(true) },
    );
    const previous = command("Go to previous message");
    expect([previous.shortcut, command("Go to next message").shortcut]).toEqual([
      "message.previous",
      "message.next",
    ]);
    act(() => previous.run());
    act(() => command("Go to next message").run());
    expect([messageNav.goPrev.mock.calls.length, messageNav.goNext.mock.calls.length]).toEqual([
      1, 1,
    ]);

    rerender(
      <TranscriptPaletteCommands conversationId="c1" bubbles={[]} messageNav={messageNav} />,
    );
    for (const label of ["Go to previous message", "Go to next message"]) {
      expect(command(label).availability).toEqual(unavailableBecause(NO_PROMPT_YET));
    }
  });
});

function userBlock(itemId: string, content: UserMessageBlock["content"]): UserMessageBlock {
  return {
    type: "user_message",
    ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: itemId, itemId },
    content,
  };
}

describe("TranscriptPaletteCommands in the transcript", () => {
  it("copies the latest prompt the transcript shows, not a hidden message from another session", async () => {
    writePeerSessionMessageDisplay("hide");
    useChatStore.setState({
      conversationId: "c1",
      blocks: [
        userBlock("u1", [{ type: "input_text", text: "Fix the parser" }]),
        userBlock("u2", [
          { type: "input_text", text: "[Message from session peer_1]\nStatus from the peer" },
        ]),
      ],
    });
    const FlagOn = withFlag(true);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/c/c1"]}>
          <FlagOn>
            <Transcript
              setConversationEl={vi.fn()}
              containerEl={null}
              scroller={null}
              setScroller={vi.fn()}
              sendScrollNonce={0}
              hasMoreHistory={false}
              loadingMoreHistory={false}
              isMobileViewport={false}
              showsWorking={false}
              agentsError={null}
              sandboxLaunching={false}
              spacerMeasureRef={{ current: null }}
            />
          </FlagOn>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await act(async () => command("Copy latest prompt").run());
    expect(clipboard.text).toHaveBeenLastCalledWith("Fix the parser");
    await act(async () => command("Copy link to latest prompt").run());
    expect(clipboard.text).toHaveBeenLastCalledWith(expect.stringContaining("message=u1"));
  });
});

describe("TranscriptPaletteCommands for an attachment-only prompt", () => {
  it("greys Copy latest prompt and never empties the clipboard, but still links to it", async () => {
    const attachmentOnly: Bubble[] = [
      { kind: "user", itemId: "u1", content: [{ type: "input_image", image_url: "data:," }] },
    ];
    render(<TranscriptPaletteCommands conversationId="c1" bubbles={attachmentOnly} />, {
      wrapper: withFlag(true),
    });
    const copy = command("Copy latest prompt");
    expect(copy.availability?.available).toBe(false);
    await act(async () => copy.run());
    expect(clipboard.text).not.toHaveBeenCalled();
    await act(async () => command("Copy link to latest prompt").run());
    expect(clipboard.text).toHaveBeenLastCalledWith(expect.stringContaining("message=u1"));
  });
});

describe("useStopResponsePaletteCommand", () => {
  it("offers Stop response only while a turn runs and the viewer may send", () => {
    const stop = vi.fn();
    const { rerender } = renderHook(
      ({ working, readOnly }: { working: boolean; readOnly: boolean }) =>
        useStopResponsePaletteCommand({ conversationId: "c1", working, readOnly, stop }),
      { wrapper: withFlag(true), initialProps: { working: true, readOnly: false } },
    );
    act(() => command("Stop response").run());
    expect(stop).toHaveBeenCalledTimes(1);
    rerender({ working: true, readOnly: true });
    expect(command("Stop response").availability?.available).toBe(false);
    rerender({ working: false, readOnly: false });
    expect(command("Stop response").availability?.available).toBe(false);
  });
});

/** The real transcript of conversation c1, holding whatever `useChatStore` holds. */
function renderTranscript(quickPick: boolean) {
  const Flag = withFlag(quickPick);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/c/c1"]}>
        <Flag>
          <Transcript
            setConversationEl={vi.fn()}
            containerEl={null}
            scroller={null}
            setScroller={vi.fn()}
            sendScrollNonce={0}
            hasMoreHistory={false}
            loadingMoreHistory={false}
            isMobileViewport={false}
            showsWorking={false}
            agentsError={null}
            sandboxLaunching={false}
            spacerMeasureRef={{ current: null }}
          />
        </Flag>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Dispatches a keydown on the window and says whether something handled it. */
function press(init: KeyboardEventInit): boolean {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}

describe("Transcript message steps", () => {
  // Pinned off macOS, where the steps' chords use Ctrl.
  beforeEach(() => {
    platform.mac = false;
  });

  it.each([
    [false, true],
    [true, false],
  ])(
    "steps messages on Ctrl+Alt+Shift+↑ off macOS only with quick_pick_palette off (on: %s)",
    (quickPick, shiftSteps) => {
      useChatStore.setState({
        conversationId: "c1",
        blocks: [userBlock("u1", [{ type: "input_text", text: "Fix the parser" }])],
      });
      renderTranscript(quickPick);
      // Ctrl+Alt+↑ steps either way; with the flag, Shift added is Previous session's.
      expect(press({ key: "ArrowUp", ctrlKey: true, altKey: true })).toBe(true);
      expect(press({ key: "ArrowUp", ctrlKey: true, altKey: true, shiftKey: true })).toBe(
        shiftSteps,
      );
    },
  );

  it("steps from ⌘K and the keyboard through one shared position", () => {
    useChatStore.setState({
      conversationId: "c1",
      blocks: [
        userBlock("u1", [{ type: "input_text", text: "Fix the parser" }]),
        userBlock("u2", [{ type: "input_text", text: "Now the lexer" }]),
      ],
    });
    const landedOn: (string | undefined)[] = [];
    vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(function (this: Element) {
      landedOn.push(this.closest<HTMLElement>("[data-user-message-id]")?.dataset.userMessageId);
    });
    renderTranscript(true);

    act(() => command("Go to previous message").run());
    act(() => {
      press({ key: "ArrowUp", ctrlKey: true, altKey: true });
    });
    act(() => command("Go to next message").run());
    expect(landedOn).toEqual(["u2", "u1", "u2"]);
  });
});
