import { describe, expect, it } from "vitest";
import { BlockStream } from "./blockStream";
import {
  computerFramesFromWire,
  computerUsePresentationFromWire,
  computerUseViewModelsEqual,
  deriveComputerUseViewModel,
} from "./computerUse";
import type { ConversationItem } from "./conversationItems";
import type { StreamEvent } from "./events";
import { itemsToBlocks } from "./itemsToBlocks";

const presentationWire = {
  kind: "computer_use" as const,
  provider: "codex" as const,
  app_name: "TextEdit",
  app_id: "com.apple.TextEdit",
  action_label: "Inspect document",
  action_kinds: ["inspect" as const],
};

const presentation = {
  kind: "computer_use" as const,
  provider: "codex" as const,
  appName: "TextEdit",
  appId: "com.apple.TextEdit",
  actionLabel: "Inspect document",
  actionKinds: ["inspect" as const],
};

const frameWire = {
  kind: "computer_frame" as const,
  file_id: "file_frame_1",
  content_type: "image/png" as const,
  width: 1280,
  height: 800,
};

const frame = {
  kind: "computer_frame" as const,
  fileId: "file_frame_1",
  contentType: "image/png" as const,
  width: 1280,
  height: 800,
};

function historyItems(): ConversationItem[] {
  return [
    {
      id: "fc_1",
      type: "function_call",
      response_id: "resp_1",
      status: "completed",
      name: "mcp__node_repl__js",
      arguments: "{}",
      call_id: "call_1",
      presentation: presentationWire,
    },
    {
      id: "fco_1",
      type: "function_call_output",
      response_id: "resp_1",
      status: "completed",
      call_id: "call_1",
      output: "captured",
      attachments: [frameWire],
      presentation: presentationWire,
      presentation_final: true,
    },
  ];
}

function liveEvents(): StreamEvent[] {
  return [
    {
      type: "tool_call",
      name: "mcp__node_repl__js",
      arguments: {},
      callId: "call_1",
      status: "completed",
      agentName: "codex-native-ui",
      itemId: "fc_1",
      responseId: "resp_1",
      presentation,
    },
    {
      type: "tool_result",
      callId: "call_1",
      output: "captured",
      itemId: "fco_1",
      responseId: "resp_1",
      attachments: [frame],
      presentation,
      presentationFinal: true,
      status: "completed",
    },
  ];
}

describe("computer-use wire parsing", () => {
  it("normalizes bounded presentation metadata and complete frame references", () => {
    expect(computerUsePresentationFromWire(presentationWire)).toEqual(presentation);
    expect(computerFramesFromWire([frameWire])).toEqual([frame]);
  });

  it("drops unknown providers and malformed or unsupported frames", () => {
    expect(
      computerUsePresentationFromWire({ kind: "computer_use", provider: "unknown" }),
    ).toBeUndefined();
    expect(
      computerFramesFromWire([
        { ...frameWire, file_id: "" },
        { ...frameWire, content_type: "image/gif" },
        { ...frameWire, width: 0 },
      ]),
    ).toEqual([]);

    expect(
      computerUsePresentationFromWire({
        ...presentationWire,
        action_kinds: ["click", "unknown", "click", "scroll"],
      }),
    ).toMatchObject({ actionKinds: ["click", "scroll"] });
  });
});

describe("deriveComputerUseViewModel", () => {
  it("produces identical state from hydrated history and live stream blocks", () => {
    const history = deriveComputerUseViewModel(itemsToBlocks(historyItems()));
    const live = deriveComputerUseViewModel(new BlockStream().reduceSync(liveEvents()));

    expect(live).toEqual(history);
    expect(history).toEqual({
      callId: "call_1",
      presentation,
      status: "completed",
      frame,
      error: null,
    });
  });

  it("shows a classified call as running before its result arrives", () => {
    const blocks = itemsToBlocks(historyItems().slice(0, 1));
    expect(deriveComputerUseViewModel(blocks)?.status).toBe("running");
  });

  it("clears a provisional false positive when the terminal result is final", () => {
    const items = historyItems();
    const output = items[1] as Record<string, unknown>;
    delete output.presentation;
    delete output.attachments;

    expect(deriveComputerUseViewModel(itemsToBlocks(items))).toBeNull();
  });

  it("retains provisional classification for an interrupted call", () => {
    const items = historyItems();
    const output = items[1] as Record<string, unknown>;
    delete output.presentation;
    delete output.attachments;
    output.presentation_final = false;
    output.status = "interrupted";

    expect(deriveComputerUseViewModel(itemsToBlocks(items))).toMatchObject({
      callId: "call_1",
      presentation,
      status: "interrupted",
    });
  });

  it("retains the latest prior frame across a later text-only action", () => {
    const items = historyItems();
    items.push(
      {
        id: "fc_2",
        type: "function_call",
        response_id: "resp_1",
        status: "completed",
        name: "mcp__node_repl__js",
        arguments: "{}",
        call_id: "call_2",
        presentation: { ...presentationWire, action_label: "Read window title" },
      },
      {
        id: "fco_2",
        type: "function_call_output",
        response_id: "resp_1",
        status: "completed",
        call_id: "call_2",
        output: "Untitled",
        presentation: { ...presentationWire, action_label: "Read window title" },
        presentation_final: true,
      },
    );

    expect(deriveComputerUseViewModel(itemsToBlocks(items))).toMatchObject({
      callId: "call_2",
      status: "completed",
      frame,
      presentation: { actionLabel: "Read window title" },
    });
  });

  it.each(["completed", "failed", "interrupted"] as const)(
    "preserves the %s terminal lifecycle",
    (status) => {
      const items = historyItems();
      items[1]!.status = status;
      if (status === "failed") (items[1] as { output: string }).output = "Screen capture failed";

      const model = deriveComputerUseViewModel(itemsToBlocks(items));
      expect(model?.status).toBe(status);
      expect(model?.error).toBe(status === "failed" ? "Screen capture failed" : null);
    },
  );
});

describe("computerUseViewModelsEqual", () => {
  it("detects a change in the summarized computer actions", () => {
    const model = deriveComputerUseViewModel(itemsToBlocks(historyItems()));
    expect(model).not.toBeNull();
    if (model === null) return;

    expect(
      computerUseViewModelsEqual(model, {
        ...model,
        presentation: { ...model.presentation, actionKinds: ["click"] },
      }),
    ).toBe(false);
  });
});
