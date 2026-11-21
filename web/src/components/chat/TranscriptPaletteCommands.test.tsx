import { act, cleanup, render, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { StickToBottom } from "use-stick-to-bottom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type { Bubble } from "@/lib/renderItems";
import { ForkDialogContextProvider } from "@/shell/ForkDialogContext";
import { type PaletteCommand, usePaletteCommandsStore } from "@/store/paletteCommands";

import { useStopResponsePaletteCommand } from "./stopResponse";
import { TranscriptPaletteCommands } from "./TranscriptPaletteCommands";

const clipboard = vi.hoisted(() => ({
  text: vi.fn(async (_text: string) => {}),
  markdown: vi.fn(async (_text: string) => {}),
}));
vi.mock("@/lib/clipboard", () => ({ copyText: clipboard.text }));
vi.mock("@/lib/copyMarkdown", () => ({ copyMarkdown: clipboard.markdown }));

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
  vi.clearAllMocks();
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

  it("forks from the latest response through the fork dialog", () => {
    const openForkDialog = vi.fn();
    render(<TranscriptPaletteCommands conversationId="c1" bubbles={BUBBLES} />, {
      wrapper: withFlag(true, openForkDialog),
    });
    act(() => command("Fork from latest response…").run());
    expect(openForkDialog).toHaveBeenCalledWith({ upToResponseId: "r1" });
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
