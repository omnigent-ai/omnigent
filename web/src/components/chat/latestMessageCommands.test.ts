import { describe, expect, it } from "vitest";

import type { Bubble } from "@/lib/renderItems";
import { isCommandAvailable, isCommandListed } from "@/store/paletteCommands";

import {
  type LatestMessageCommandId,
  latestMessageCommands,
  latestMessages,
  NO_PROMPT_TEXT,
  NO_PROMPT_YET,
  NO_RESPONSE_TEXT,
  NO_RESPONSE_YET,
  PROMPT_NOT_SENT,
} from "./latestMessageCommands";

type UserBubble = Extract<Bubble, { kind: "user" }>;
type AssistantBubble = Extract<Bubble, { kind: "assistant" }>;

const prompt = (itemId: string, extra: Partial<UserBubble> = {}): Bubble => ({
  kind: "user",
  itemId,
  content: [{ type: "input_text", text: `Prompt ${itemId}` }],
  ...extra,
});
const response = (
  responseId: string,
  lifecycle: AssistantBubble["lifecycle"] = "completed",
): Bubble => ({
  kind: "assistant",
  responseId,
  stableId: responseId,
  lifecycle,
  error: null,
  items: [],
});

interface Fork {
  canFork: boolean;
  disabledReason?: string;
}

function commandsFor(
  bubbles: Bubble[],
  { responseHasText = true, promptHasText = true, fork = { canFork: true } as Fork | null } = {},
) {
  const commands = latestMessageCommands({
    latest: latestMessages(bubbles),
    responseHasText,
    promptHasText,
    fork,
  });
  const find = (id: LatestMessageCommandId) => commands.find((command) => command.id === id)!;
  return {
    listed: commands.filter((command) => isCommandListed(command.availability)).map(({ id }) => id),
    available: (id: LatestMessageCommandId) => isCommandAvailable(find(id).availability),
    reason: (id: LatestMessageCommandId) => {
      const { availability } = find(id);
      return availability.available ? undefined : availability.reason;
    },
  };
}

describe("latestMessages", () => {
  it("finds the newest response and the newest prompt, skipping runtime notices", () => {
    const notice = prompt("n1", {
      content: [{ type: "input_text", text: "[System: session resumed]" }],
    });
    const latest = latestMessages([
      prompt("u1"),
      response("r1"),
      prompt("u2"),
      response("r2"),
      notice,
    ]);
    expect(latest.response).toMatchObject({ responseId: "r2" });
    expect(latest.prompt).toMatchObject({ itemId: "u2" });
  });
});

describe("latestMessageCommands", () => {
  it("offers every command once a response and a sent prompt exist", () => {
    const commands = commandsFor([prompt("u1"), response("r1")]);
    expect(commands.listed).toEqual([
      "copy-latest-response",
      "copy-latest-response-link",
      "fork-latest-response",
      "copy-latest-prompt",
      "copy-latest-prompt-link",
    ]);
    expect(commands.listed.every(commands.available)).toBe(true);
  });

  it("says why when there's nothing to act on yet", () => {
    const empty = commandsFor([]);
    expect(empty.reason("copy-latest-response")).toBe(NO_RESPONSE_YET);
    expect(empty.reason("fork-latest-response")).toBe(NO_RESPONSE_YET);
    expect(empty.reason("copy-latest-prompt")).toBe(NO_PROMPT_YET);
    const noText = commandsFor([response("r1")], { responseHasText: false });
    expect(noText.reason("copy-latest-response")).toBe(NO_RESPONSE_TEXT);
    // A prompt of only attachments has nothing to copy, but its link still works.
    const attachmentsOnly = commandsFor([prompt("u1")], { promptHasText: false });
    expect(attachmentsOnly.reason("copy-latest-prompt")).toBe(NO_PROMPT_TEXT);
    expect(attachmentsOnly.available("copy-latest-prompt-link")).toBe(true);
    const unsent = commandsFor([prompt("u1", { pending: true })]);
    expect(unsent.reason("copy-latest-prompt-link")).toBe(PROMPT_NOT_SENT);
  });

  it("forks as the response's own button does: hidden while streaming or unforkable", () => {
    expect(commandsFor([response("r1", "streaming")]).listed).not.toContain("fork-latest-response");
    const unforkable = commandsFor([response("r1")], { fork: { canFork: false } });
    expect(unforkable.listed).not.toContain("fork-latest-response");
    expect(commandsFor([], { fork: null }).listed).not.toContain("fork-latest-response");
    const greyed = commandsFor([response("r1")], {
      fork: { canFork: true, disabledReason: "Fork is off." },
    });
    expect(greyed.reason("fork-latest-response")).toBe("Fork is off.");
  });
});
