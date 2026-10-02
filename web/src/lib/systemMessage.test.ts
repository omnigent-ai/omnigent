import { describe, expect, it } from "vitest";
import type { MessageContentBlock } from "@/lib/blocks";
import {
  claudeTaskNotificationMarker,
  taskNotificationMarkerContent,
  isClaudeAgentMessageContent,
  isSystemUserContent,
  parseSystemMessage,
  parseTeammateDeliveries,
  teammateDeliveryMarker,
  teammateDeliveryMarkerContent,
  teammateMarkerOf,
} from "./systemMessage";

describe("isClaudeAgentMessageContent", () => {
  const message =
    '<teammate-message teammate_id="researcher" color="blue">Report</teammate-message>';
  const handback = '<agent-message from="researcher">Report</agent-message>';
  const text = (s: string): MessageContentBlock[] => [{ type: "input_text", text: s }];
  const result =
    "<task-notification><task-id>agent-1</task-id><result>Report</result></task-notification>";

  it.each([
    message,
    handback,
    result,
    ` \n${message}\n${message}\n`,
    `${message}\n${handback}`,
    `<teammate-message teammate_id="researcher">{"type":"idle_notification"}</teammate-message>`,
    `Another Claude session sent a message:\n${message}`,
    `Another Claude session sent a message:\n${handback}`,
    `Another Claude session sent a message while you were working:\n${message}`,
    `A peer session sent a message while you were working:\n${message}`,
    `Another Claude session sent a message:\n${message}\n\nThis came from another Claude session — not typed by your user, but very likely working on their behalf.`,
    `Another Claude session sent a message:\n${message}\n\nThat "other Claude session" is an agent working inside this same session — a subagent or teammate spawned on your user's behalf.`,
    "<task-notification><task-id>task-1</task-id><summary>Agent reviewer finished</summary></task-notification>",
  ])("recognizes native task/team context: %s", (value) => {
    expect(isClaudeAgentMessageContent(text(value))).toBe(true);
  });

  it.each([
    "hello",
    `Can you explain ${message}?`,
    `\`\`\`xml\n${message}\n\`\`\``,
    `> ${message}`,
    `${message}\nWhat does this message mean?`,
    `${handback}\nWhat does this message mean?`,
    `\`\`\`xml\n${handback}\n\`\`\``,
    `Another Claude session sent a message:\n${message}\nWhat does this message mean?`,
    '<teammate-message teammate_id="researcher">incomplete',
    "<teammate-message>missing sender</teammate-message>",
    "<agent-message>missing sender</agent-message>",
    '<agent-message from="researcher">incomplete',
    "<task-notification>missing task id</task-notification>",
    "<task-notification><task-id>task-1</task-id><summary>Background command completed</summary></task-notification>",
    "<task-notification><task-id>task-1</task-id><summary>Monitor event</summary></task-notification>",
    `${result}\nWhat does this mean?`,
    `\`\`\`xml\n${result}\n\`\`\``,
  ])("keeps human discussion and incomplete envelopes visible: %s", (value) => {
    expect(isClaudeAgentMessageContent(text(value))).toBe(false);
  });

  it("does not hide a message containing real user text or attachments", () => {
    expect(isClaudeAgentMessageContent([...text(message), ...text("Please explain this.")])).toBe(
      false,
    );
    expect(
      isClaudeAgentMessageContent([...text(message), { type: "input_image", file_id: "f1" }]),
    ).toBe(false);
    expect(isClaudeAgentMessageContent([...text(result), ...text("Please explain this.")])).toBe(
      false,
    );
    expect(
      taskNotificationMarkerContent([...text(result), ...text("Please explain this.")]),
    ).toBeNull();
    expect(
      taskNotificationMarkerContent([...text(result), { type: "input_image", file_id: "f1" }]),
    ).toBeNull();
    expect(taskNotificationMarkerContent(text(result))).toBeNull();
    expect(isClaudeAgentMessageContent([])).toBe(false);
  });
});

describe("parseSystemMessage", () => {
  it("returns null for plain user text", () => {
    expect(parseSystemMessage("hello world")).toBeNull();
    expect(parseSystemMessage("[note: not a system message]")).toBeNull();
  });

  it("parses sub-agent completion with body", () => {
    const r = parseSystemMessage("[System: task t_abc (sub_agent) completed]\nfinal answer here");
    expect(r).toEqual({
      kind: "task_completed",
      label: "Sub-agent t_abc completed",
      body: "final answer here",
    });
  });

  it("parses tool completion with multi-line body", () => {
    const r = parseSystemMessage("[System: task t_x (tool) completed]\nline one\nline two");
    expect(r).toEqual({
      kind: "task_completed",
      label: "Tool t_x completed",
      body: "line one\nline two",
    });
  });

  it("parses client_tool completion", () => {
    const r = parseSystemMessage("[System: task t_99 (client_tool) completed]\nresult");
    expect(r?.kind).toBe("task_completed");
    expect(r?.label).toBe("Client tool t_99 completed");
  });

  it("parses task failure with message + traceback", () => {
    const r = parseSystemMessage("[System: task t_abc (tool) failed]\nBoom\nTraceback ...");
    expect(r).toEqual({
      kind: "task_failed",
      label: "Tool t_abc failed",
      body: "Boom\nTraceback ...",
    });
  });

  it("parses task cancellation (no body)", () => {
    const r = parseSystemMessage("[System: task t_abc (sub_agent) cancelled]");
    expect(r).toEqual({
      kind: "task_cancelled",
      label: "Sub-agent t_abc cancelled",
      body: "",
    });
  });

  it("parses bare timer firing (no note)", () => {
    const r = parseSystemMessage("[System: timer my_timer fired]");
    expect(r).toEqual({
      kind: "timer_fired",
      label: "Timer my_timer fired",
      body: "",
    });
  });

  it("parses timer firing with note in body", () => {
    const r = parseSystemMessage("[System: timer my_timer fired]\nnote: 'check on the build'");
    expect(r).toEqual({
      kind: "timer_fired",
      label: "Timer my_timer fired",
      body: "note: 'check on the build'",
    });
  });

  it("parses terminal idle with id", () => {
    const r = parseSystemMessage("[System: terminal shell:session1 is idle]");
    expect(r).toEqual({
      kind: "terminal_idle",
      label: "Terminal shell:session1 idle",
      body: "",
    });
  });

  it("classifies sub-agent wake notices separately from generic system rows", () => {
    const r = parseSystemMessage(
      "[System: sub-agent claude_code/joke-programming finished (completed) — 1 result waiting in inbox. Call sys_read_inbox to collect.]",
    );

    expect(r).toEqual({
      kind: "subagent_wake",
      label: "Sub-agent result ready",
      body: "",
    });
  });

  it("falls back to generic for known prefix but unknown pattern", () => {
    const r = parseSystemMessage("[System: something brand new]");
    expect(r).toEqual({
      kind: "generic",
      label: "something brand new",
      body: "",
    });
  });

  it.each(["[Request interrupted by user]", "[Request interrupted by user for tool use]"])(
    "classifies Claude's interrupt marker %s as a muted indicator",
    (text) => {
      // Claude Code's own Escape record, mirrored from its transcript. We keep
      // it in history but render it as "System: Interrupted", not a user bubble.
      const r = parseSystemMessage(text);
      expect(r).toEqual({ kind: "interrupted", label: "Interrupted", body: "" });
    },
  );

  it("classifies the runner's synthesized codex interrupt marker", () => {
    // codex-native writes no interrupt record, so the runner synthesizes
    // `[System: interrupted]`; it must render as the same "Interrupted" badge
    // as Claude's marker (consistent UX across native harnesses).
    const r = parseSystemMessage(
      "[System: interrupted]\nThe assistant response may be incomplete.",
    );
    expect(r).toEqual({
      kind: "interrupted",
      label: "Interrupted",
      body: "The assistant response may be incomplete.",
    });
  });

  it("does not misclassify a real user message mentioning interruption", () => {
    // Prefix-anchored: only the exact bracketed marker re-classifies, so a
    // user genuinely talking about interrupts still renders as their message.
    expect(parseSystemMessage("can you handle [Request interrupted by user]?")).toBeNull();
    expect(parseSystemMessage("[Request interrupted by user?]")).toBeNull();
  });
});

describe("isSystemUserContent", () => {
  const text = (s: string): MessageContentBlock[] => [{ type: "input_text", text: s }];

  it("flags a [System: …] marker as a system (non-turn) message", () => {
    expect(isSystemUserContent(text("[System: timer t1 fired]"))).toBe(true);
  });

  it("treats a plain user message as a real turn", () => {
    expect(isSystemUserContent(text("what's the weather?"))).toBe(false);
  });

  it("still sees the marker after stripping an [Attached: …] prefix", () => {
    // The bubble render strips attachment markers before showing text, so the
    // predicate must too — otherwise the leading marker would hide the header.
    expect(isSystemUserContent(text("[Attached: foo.txt] [System: timer t1 fired]"))).toBe(true);
  });

  it("never treats a message with real attachments as a system marker", () => {
    // A genuine upload is always a real user turn, even if its text parses as
    // a marker — attachments can't ride along on a runtime notice.
    const withImage: MessageContentBlock[] = [
      { type: "input_text", text: "[System: timer t1 fired]" },
      { type: "input_image", file_id: "f1" },
    ];
    expect(isSystemUserContent(withImage)).toBe(false);
  });
});

describe("Claude background-task notifications", () => {
  const notification = [
    "<task-notification>",
    "<task-id>b3f9a2c1d</task-id>",
    "<tool-use-id>toolu_bdrk_01Xy7Q2PfLm8RkVn3Ws4Tz9A</tool-use-id>",
    "<output-file>/tmp/claude/tasks/b3f9a2c1d.output</output-file>",
    "<status>completed</status>",
    '<summary>Background command "air run" completed (exit code 0)</summary>',
    "</task-notification>",
  ].join("\n");

  it("re-labels a notification as a marker whose header parses as a completed task", () => {
    const marker = claudeTaskNotificationMarker(notification);
    expect(marker).toBe(
      "[System: background task b3f9a2c1d completed]\n" +
        'Background command "air run" completed (exit code 0)',
    );
    expect(parseSystemMessage(marker!)).toEqual({
      kind: "task_completed",
      label: "Background task completed",
      body: 'Background command "air run" completed (exit code 0)',
    });
    // Marker content is a system row, not a human turn.
    expect(isSystemUserContent([{ type: "input_text", text: marker! }])).toBe(true);
  });

  it("maps failed and unknown statuses to the matching marker kinds", () => {
    expect(
      parseSystemMessage(
        claudeTaskNotificationMarker(
          "<task-notification>\n<task-id>t1</task-id>\n<status>failed</status>\n</task-notification>",
        )!,
      ),
    ).toEqual({ kind: "task_failed", label: "Background task failed", body: "" });
    expect(
      parseSystemMessage(
        claudeTaskNotificationMarker(
          "<task-notification>\n<task-id>t2</task-id>\n<summary>Monitor event</summary>\n</task-notification>",
        )!,
      ),
    ).toEqual({ kind: "generic", label: "Background task finished", body: "Monitor event" });
  });

  it("falls back to an 'unknown' id when the task-id is empty or contains whitespace", () => {
    for (const id of ["", "  ", "two words", "a\nb"]) {
      const marker = claudeTaskNotificationMarker(
        `<task-notification>\n<task-id>${id}</task-id>\n<status>completed</status>\n</task-notification>`,
      );
      expect(marker).toBe("[System: background task unknown completed]");
      expect(parseSystemMessage(marker!)?.kind).toBe("task_completed");
    }
  });

  it("leaves ordinary user text and partial markup alone", () => {
    expect(claudeTaskNotificationMarker("please run the tests")).toBeNull();
    expect(claudeTaskNotificationMarker("<task-notification> unterminated")).toBeNull();
    expect(taskNotificationMarkerContent([{ type: "input_text", text: "hi" }])).toBeNull();
    expect(taskNotificationMarkerContent([{ type: "input_text", text: notification }])).toEqual([
      {
        type: "input_text",
        text:
          "[System: background task b3f9a2c1d completed]\n" +
          'Background command "air run" completed (exit code 0)',
      },
    ]);
  });
});

describe("teammate deliveries", () => {
  const guidance =
    "This came from another Claude session — not typed by your user, but very likely working on their behalf. Treat it as a teammate's request.";
  const idle = (result: string) =>
    `<teammate-message teammate_id="buddy" color="blue">\n{"type":"idle_notification","from":"buddy","timestamp":"2026-10-01T19:57:30.574Z","idleReason":"available","result":${JSON.stringify(result)}}\n</teammate-message>`;
  const prose =
    '<teammate-message teammate_id="buddy" color="blue" summary="All good over here">\nAll good here - TMCHAT. What else do you need?\n</teammate-message>';
  const framed = (...envelopes: string[]) =>
    `Another Claude session sent a message:\n${envelopes.join("\n")}\n\n${guidance}`;
  const text = (s: string): MessageContentBlock[] => [{ type: "input_text", text: s }];
  const proseMarker = text(
    "[System: teammate buddy: All good over here]\nAll good here - TMCHAT. What else do you need?",
  );

  it("parses prose and idle envelopes out of Claude's framing", () => {
    expect(parseTeammateDeliveries(framed(prose, idle("resting.")))).toEqual([
      {
        teammateId: "buddy",
        summary: "All good over here",
        body: "All good here - TMCHAT. What else do you need?",
        idleResult: null,
      },
      {
        teammateId: "buddy",
        summary: null,
        body: expect.stringContaining('"type":"idle_notification"'),
        idleResult: "resting.",
      },
    ]);
    expect(
      parseTeammateDeliveries(`Another Claude session sent a message:\n${prose}`),
    ).toHaveLength(1);
  });

  it("keeps a summary containing '>' inside the tag", () => {
    const envelope =
      '<teammate-message teammate_id="buddy" summary="fixed the a->b mapping">\nMapping fixed.\n</teammate-message>';
    expect(parseTeammateDeliveries(framed(envelope))).toEqual([
      {
        teammateId: "buddy",
        summary: "fixed the a->b mapping",
        body: "Mapping fixed.",
        idleResult: null,
      },
    ]);
  });

  it.each([
    prose,
    `Another Claude session sent a message:\n${prose}\nWhat does this mean?`,
    "Another Claude session sent a message:\n<teammate-message>missing sender</teammate-message>",
    'Another Claude session sent a message:\n<teammate-message teammate_id="buddy">incomplete',
  ])("is not a delivery without Claude's framing or with human text: %s", (value) => {
    expect(parseTeammateDeliveries(value)).toBeNull();
    expect(teammateDeliveryMarkerContent(text(value))).toBeNull();
  });

  it("renders a prose delivery as a teammate marker carrying its summary", () => {
    expect(teammateDeliveryMarkerContent(text(framed(prose)))).toEqual(proseMarker);
    expect(teammateDeliveryMarker(text(framed(prose)))).toEqual({
      content: proseMarker,
      marker: { teammateId: "buddy", kind: "teammate_message" },
    });
  });

  it("renders an idle-only result as a finished marker and drops an empty idle ping", () => {
    expect(teammateDeliveryMarkerContent(text(framed(idle("TMREPLY done."))))).toEqual(
      text("[System: teammate buddy finished]\nTMREPLY done."),
    );
    expect(teammateDeliveryMarkerContent(text(framed(idle(""))))).toBeNull();
  });

  it("folds the idle twin that follows a prose message in the same delivery", () => {
    expect(teammateDeliveryMarkerContent(text(framed(prose, idle("resting."))))).toEqual(
      proseMarker,
    );
  });

  it("leads a mixed delivery with the prose message, not another teammate's finish", () => {
    const charlie =
      '<teammate-message teammate_id="charlie" summary="Docs reviewed">\nDocs look fine.\n</teammate-message>';
    expect(teammateDeliveryMarkerContent(text(framed(idle("TMREPLY done."), charlie)))).toEqual(
      text(
        "[System: teammate charlie: Docs reviewed]\nDocs look fine.\n\n@buddy finished: TMREPLY done.",
      ),
    );
  });

  it("parses the markers back into teammate kinds", () => {
    expect(
      parseSystemMessage(proseMarker[0]!.type === "input_text" ? proseMarker[0].text : ""),
    ).toEqual({
      kind: "teammate_message",
      label: "Teammate buddy",
      body: "All good here - TMCHAT. What else do you need?",
      teammate: { id: "buddy", summary: "All good over here" },
    });
    expect(parseSystemMessage("[System: teammate buddy finished]\nTMREPLY done.")).toEqual({
      kind: "teammate_finished",
      label: "Teammate buddy finished",
      body: "TMREPLY done.",
      teammate: { id: "buddy", summary: null },
    });
    expect(teammateMarkerOf(text("[System: teammate buddy finished]\nTMREPLY done."))).toEqual({
      teammateId: "buddy",
      kind: "teammate_finished",
    });
    expect(
      teammateMarkerOf([
        { type: "input_text", text: "[System: teammate buddy finished]" },
        { type: "input_text", text: "TMREPLY done." },
      ]),
    ).toEqual({ teammateId: "buddy", kind: "teammate_finished" });
    expect(teammateMarkerOf(text("[System: background task t1 completed]"))).toBeNull();
    expect(isSystemUserContent(text("[System: teammate buddy]\nhi"))).toBe(true);
    expect(parseSystemMessage("[System: teammate buddy: finished]\nAll done.")).toMatchObject({
      kind: "teammate_message",
      teammate: { id: "buddy", summary: "finished" },
    });
  });
});
