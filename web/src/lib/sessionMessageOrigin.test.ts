import { describe, expect, it } from "vitest";

import type { RenderItem, ToolState } from "./renderItems";
import { outboundSessionMessage, splitSessionMessageOrigin } from "./sessionMessageOrigin";

describe("splitSessionMessageOrigin", () => {
  it("splits the sender id off a sys_session_message delivery", () => {
    expect(
      splitSessionMessageOrigin(
        "[Message from session 9b8b26c8edfd40d781c0d13a04fa5c0b]\nhi\nthere",
      ),
    ).toEqual({ sourceId: "9b8b26c8edfd40d781c0d13a04fa5c0b", body: "hi\nthere" });
  });

  it("ignores the prefix anywhere but the start, or without its newline", () => {
    expect(splitSessionMessageOrigin("hi [Message from session abc]\nthere")).toBeNull();
    expect(splitSessionMessageOrigin("[Message from session abc] hi")).toBeNull();
    expect(splitSessionMessageOrigin("plain text")).toBeNull();
  });
});

function toolCall(
  name: string,
  args: Record<string, unknown>,
  output: string | null,
  state: ToolState = output === null ? "input-available" : "output-available",
): RenderItem {
  return {
    kind: "tool",
    itemId: "fc_1",
    execution: {
      name,
      arguments: args,
      argsSummary: "",
      callId: "c_1",
      agentName: "manager",
      executedBy: "server",
      output,
    },
    output,
    state,
    startedAt: null,
    duration: undefined,
  };
}

const MESSAGE_ARGS = { session_id: "s_worker", message: "Reproduce the bug." };
const UNCONFIRMED = "Delivery could not be confirmed; do not automatically resend";

describe("outboundSessionMessage", () => {
  it.each([
    ["an accepted send", '{"queued": true, "item_id": "i_1"}', "sent", null],
    ["a send the server handled synchronously", '{"queued": false}', "sent", null],
    [
      "a native harness's wrapped acknowledgement",
      JSON.stringify([{ type: "text", text: '{"queued": true}' }]),
      "sent",
      null,
    ],
    [
      "a refusal the runner marks",
      '{"error": "Session message refused", "delivery": "refused"}',
      "not_sent",
      "Session message refused",
    ],
    [
      "a policy denial",
      '{"queued": false, "denied": true, "reason": "Needs approval", ' +
        '"error": "Session message denied by policy: Needs approval"}',
      "not_sent",
      "Session message denied by policy: Needs approval",
    ],
    [
      "a policy denial without an error",
      '{"queued": false, "denied": true, "reason": "Needs approval"}',
      "not_sent",
      "Needs approval",
    ],
    [
      "an unconfirmed delivery",
      JSON.stringify({ error: UNCONFIRMED, delivery: "unconfirmed" }),
      "unconfirmed",
      UNCONFIRMED,
    ],
    [
      "an older runner's unmarked refusal",
      '{"error": "Session message refused"}',
      "unconfirmed",
      "Session message refused",
    ],
    [
      "a failed tool call",
      "Error executing tool sys_session_message: relay closed",
      "unconfirmed",
      "Error executing tool sys_session_message: relay closed",
    ],
    ["an acknowledgement without queued", "{}", "unconfirmed", null],
  ])("classifies %s", (_case, output, status, error) => {
    expect(outboundSessionMessage(toolCall("sys_session_message", MESSAGE_ARGS, output))).toEqual({
      recipientId: "s_worker",
      body: "Reproduce the bug.",
      status,
      error,
    });
  });

  it.each([
    ["input-available", "sending"],
    ["cancelled", "unconfirmed"],
    ["no-output", "unconfirmed"],
    ["output-error", "unconfirmed"],
  ] as const)("treats a %s call without a result as %s", (state, status) => {
    const item = toolCall("mcp__omnigent__sys_session_message", MESSAGE_ARGS, null, state);
    expect(outboundSessionMessage(item)?.status).toBe(status);
  });

  const FIRST_TASK_FAILED = {
    conversation_id: "s_new",
    status: "created_initial_message_failed",
    error: "Session created, but its first task was refused or not confirmed",
  };

  it.each([
    [
      "an accepted same-host first task",
      "same_host",
      { conversation_id: "s_new", status: "created", initial_message: { queued: true } },
      "sent",
      null,
    ],
    [
      "a refused first task",
      "host",
      {
        ...FIRST_TASK_FAILED,
        initial_message: { error: "Session message refused", delivery: "refused" },
      },
      "not_sent",
      "Session message refused",
    ],
    [
      "an unconfirmed first task",
      "same_host",
      { ...FIRST_TASK_FAILED, initial_message: { error: UNCONFIRMED, delivery: "unconfirmed" } },
      "unconfirmed",
      UNCONFIRMED,
    ],
    [
      "a managed first task, accepted with its session",
      "managed",
      { conversation_id: "s_new", status: "created" },
      "sent",
      null,
    ],
  ])("reads %s from the create result", (_case, placement, result, status, error) => {
    const args = { agent_id: "worker", placement, message: "Start." };
    const item = toolCall("sys_session_create", args, JSON.stringify(result));
    expect(outboundSessionMessage(item)).toEqual({
      recipientId: "s_new",
      body: "Start.",
      status,
      error,
    });
  });

  const CREATED = '{"conversation_id": "s_new", "status": "created"}';

  it.each([
    ["another tool", toolCall("sys_session_get_info", { session_id: "s_worker" }, "{}")],
    [
      "another server's tool",
      toolCall("mcp__other__sys_session_message", MESSAGE_ARGS, '{"queued": true}'),
    ],
    [
      "a message without text",
      toolCall("sys_session_message", { session_id: "s_worker", message: " " }, "{}"),
    ],
    [
      "a child's first task",
      toolCall("sys_session_create", { agent_id: "worker", message: "Start." }, CREATED),
    ],
    [
      "a create without a first task",
      toolCall("sys_session_create", { agent_id: "worker", placement: "managed" }, CREATED),
    ],
    [
      "a create still running",
      toolCall("sys_session_create", { agent_id: "w", placement: "managed", message: "Go" }, null),
    ],
    [
      "a create whose session is unknown",
      toolCall(
        "sys_session_create",
        { agent_id: "worker", placement: "same_host", message: "Start." },
        '{"error": "Session creation could not be confirmed", "agent_id": "worker"}',
      ),
    ],
  ])("ignores %s", (_case, item) => {
    expect(outboundSessionMessage(item)).toBeNull();
  });
});
