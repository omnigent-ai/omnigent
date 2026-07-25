// Pin the wire envelopes for `session.*` SSE events.
//
// Each event in `omnigent/server/schemas.py` uses either a FLAT
// envelope (`{type, ...fields}`) or a NESTED envelope (`{type, data:
// {...}}`). The parser in `sse.ts` must lift each into the same
// camelCase TS interface; bugs in that lift are silent (the reducer
// just stops seeing the event). These tests fail loud when the wire
// shape drifts.

import { describe, expect, it } from "vitest";
import type {
  ElicitationRequest,
  SessionAgentChangedEvent,
  SessionCollaborationModeEvent,
  SessionChangedFilesInvalidatedEvent,
  SessionChildSessionUpdatedEvent,
  SessionModelOptionsEvent,
  SessionCreatedEvent,
  SessionInputConsumedEvent,
  SessionInterruptedEvent,
  SessionModelEvent,
  SessionPresenceEvent,
  SessionReasoningEffortEvent,
  SessionResourceCreatedEvent,
  SessionResourceDeletedEvent,
  SessionSandboxStatusEvent,
  SessionSkillsEvent,
  SessionStatusEvent,
  SessionTerminalActivityEvent,
  SessionTerminalPendingEvent,
  SessionTodosEvent,
  SessionUsageEvent,
  SlashCommand,
  RoutingDecision,
  StreamEvent,
} from "./events";
import { parseEventLines } from "./sse";

function parse(event: string, data: Record<string, unknown>): StreamEvent[] {
  return [...parseEventLines([JSON.stringify({ event, data })])];
}

describe("session.status (FLAT envelope)", () => {
  it("lifts conversation_id and status", () => {
    const out = parse("session.status", {
      type: "session.status",
      conversation_id: "conv_abc",
      status: "running",
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionStatusEvent;
    expect(ev.type).toBe("session_status");
    expect(ev.conversationId).toBe("conv_abc");
    expect(ev.status).toBe("running");
  });

  it("carries response_id when present", () => {
    const out = parse("session.status", {
      type: "session.status",
      conversation_id: "conv_abc",
      status: "running",
      response_id: "codex_turn_123",
    });
    const ev = out[0] as SessionStatusEvent;
    expect(ev.responseId).toBe("codex_turn_123");
  });

  it("accepts waiting (live-only, not on snapshot)", () => {
    const out = parse("session.status", {
      type: "session.status",
      conversation_id: "conv_abc",
      status: "waiting",
    });
    expect((out[0] as SessionStatusEvent).status).toBe("waiting");
  });

  it.each(["idle", "launching", "running", "waiting", "failed"] as const)(
    "accepts %s as a valid status",
    (status) => {
      const out = parse("session.status", {
        type: "session.status",
        conversation_id: "conv_abc",
        status,
      });
      expect(out).toHaveLength(1);
    },
  );

  it("rejects unknown status values", () => {
    const out = parse("session.status", {
      type: "session.status",
      conversation_id: "conv_abc",
      status: "bogus",
    });
    expect(out).toEqual([]);
  });

  it("rejects missing conversation_id", () => {
    const out = parse("session.status", {
      type: "session.status",
      status: "running",
    });
    expect(out).toEqual([]);
  });
});

describe("session.input.consumed (NESTED envelope)", () => {
  it("lifts item_id, type, and inner data from the nested payload", () => {
    const out = parse("session.input.consumed", {
      type: "session.input.consumed",
      data: {
        item_id: "item_abc",
        type: "message",
        data: { role: "user", content: [{ type: "input_text", text: "hi" }], is_meta: true },
      },
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionInputConsumedEvent;
    expect(ev.type).toBe("session_input_consumed");
    expect(ev.itemId).toBe("item_abc");
    expect(ev.itemType).toBe("message");
    expect(ev.isMeta).toBe(true);
    expect(ev.data).toEqual({
      role: "user",
      content: [{ type: "input_text", text: "hi" }],
      is_meta: true,
    });
  });

  it("defaults inner data to {} when missing", () => {
    const out = parse("session.input.consumed", {
      type: "session.input.consumed",
      data: { item_id: "item_abc", type: "interrupt" },
    });
    expect((out[0] as SessionInputConsumedEvent).data).toEqual({});
  });

  it("rejects when nested envelope is absent", () => {
    const out = parse("session.input.consumed", {
      type: "session.input.consumed",
      item_id: "item_abc",
      // missing `data` wrapper — must NOT silently lift from the top level
    });
    expect(out).toEqual([]);
  });

  it("lifts created_by from the payload level for live attribution", () => {
    // created_by sits beside item_id/type, NOT inside the nested item data.
    const out = parse("session.input.consumed", {
      type: "session.input.consumed",
      data: {
        item_id: "item_abc",
        type: "message",
        created_by: "bob@example.com",
        data: { role: "user", content: [{ type: "input_text", text: "hi" }] },
      },
    });
    const ev = out[0] as SessionInputConsumedEvent;
    expect(ev.createdBy).toBe("bob@example.com");
  });

  it("omits createdBy when absent or null (agent/system items)", () => {
    // null author -> no label; the field must stay undefined, not "null".
    const withNull = parse("session.input.consumed", {
      type: "session.input.consumed",
      data: { item_id: "i1", type: "message", created_by: null, data: {} },
    });
    expect((withNull[0] as SessionInputConsumedEvent).createdBy).toBeUndefined();
    const without = parse("session.input.consumed", {
      type: "session.input.consumed",
      data: { item_id: "i1", type: "message", data: {} },
    });
    expect((without[0] as SessionInputConsumedEvent).createdBy).toBeUndefined();
  });

  it("lifts cleared_pending_id from the nested payload when present", () => {
    const out = parse("session.input.consumed", {
      type: "session.input.consumed",
      data: {
        item_id: "item_abc",
        type: "message",
        data: { role: "user", content: [{ type: "input_text", text: "hi" }] },
        // Server drained this pending-input entry — the client drops
        // the matching optimistic bubble by id.
        cleared_pending_id: "pending_xyz",
      },
    });
    expect((out[0] as SessionInputConsumedEvent).clearedPendingId).toBe("pending_xyz");
  });

  it("defaults clearedPendingId to null when the payload omits it", () => {
    const out = parse("session.input.consumed", {
      type: "session.input.consumed",
      data: { item_id: "item_abc", type: "message", data: { role: "user", content: [] } },
    });
    // null (not undefined) so the store's `if (cleared)` guard reads cleanly.
    expect((out[0] as SessionInputConsumedEvent).clearedPendingId).toBeNull();
  });
});

describe("session.interrupted (NESTED envelope)", () => {
  it("lifts requested_at from the nested payload", () => {
    const out = parse("session.interrupted", {
      type: "session.interrupted",
      data: { requested_at: 1704067200 },
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionInterruptedEvent;
    expect(ev.type).toBe("session_interrupted");
    expect(ev.requestedAt).toBe(1704067200);
  });

  it("carries response_id from the nested payload", () => {
    const out = parse("session.interrupted", {
      type: "session.interrupted",
      data: { requested_at: 1704067200, response_id: "codex_turn_123" },
    });
    const ev = out[0] as SessionInterruptedEvent;
    expect(ev.responseId).toBe("codex_turn_123");
  });

  it("defaults requested_at to 0 when missing", () => {
    const out = parse("session.interrupted", {
      type: "session.interrupted",
      data: {},
    });
    expect((out[0] as SessionInterruptedEvent).requestedAt).toBe(0);
  });

  it("rejects when nested envelope is absent", () => {
    const out = parse("session.interrupted", {
      type: "session.interrupted",
      requested_at: 1704067200,
    });
    expect(out).toEqual([]);
  });
});

describe("session.created (FLAT envelope)", () => {
  it("lifts parent + child + agent ids", () => {
    const out = parse("session.created", {
      type: "session.created",
      conversation_id: "conv_parent",
      child_session_id: "conv_child",
      agent_id: "agent_xyz",
      parent_session_id: "conv_parent",
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionCreatedEvent;
    expect(ev.type).toBe("session_created");
    expect(ev.conversationId).toBe("conv_parent");
    expect(ev.childSessionId).toBe("conv_child");
    expect(ev.agentId).toBe("agent_xyz");
    expect(ev.parentSessionId).toBe("conv_parent");
  });

  it("tolerates absent agent_id (legacy spawn path)", () => {
    const out = parse("session.created", {
      type: "session.created",
      conversation_id: "conv_parent",
      child_session_id: "conv_child",
    });
    const ev = out[0] as SessionCreatedEvent;
    expect(ev.agentId).toBeNull();
    expect(ev.parentSessionId).toBeNull();
  });

  it("rejects missing child_session_id", () => {
    const out = parse("session.created", {
      type: "session.created",
      conversation_id: "conv_parent",
    });
    expect(out).toEqual([]);
  });
});

describe("session.resource.created (FLAT envelope)", () => {
  it("lifts the resource record", () => {
    const out = parse("session.resource.created", {
      type: "session.resource.created",
      resource: {
        id: "terminal_bash_s1",
        object: "session.resource",
        type: "terminal",
        session_id: "conv_abc",
        name: "bash:s1",
        environment: "env_terminal_bash_s1",
        metadata: { terminal_name: "bash", session_key: "s1", running: true },
      },
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionResourceCreatedEvent;
    expect(ev.type).toBe("session_resource_created");
    expect(ev.resource.id).toBe("terminal_bash_s1");
    expect(ev.resource.type).toBe("terminal");
    expect(ev.resource.name).toBe("bash:s1");
    // session_id must survive the parse — applyTerminalCreated routes the
    // resource into the owning session's terminal cache by this field.
    expect(ev.resource.session_id).toBe("conv_abc");
    expect(ev.resource.metadata).toEqual({
      terminal_name: "bash",
      session_key: "s1",
      running: true,
    });
  });

  it("rejects when resource is absent", () => {
    const out = parse("session.resource.created", {
      type: "session.resource.created",
    });
    expect(out).toEqual([]);
  });

  it("rejects when resource is empty (no id/type/name/metadata)", () => {
    // Per the publisher contract, a created event must carry the full
    // resource record so consumers can update local caches without
    // a follow-up REST read. An empty {} is treated as malformed.
    const out = parse("session.resource.created", {
      type: "session.resource.created",
      resource: {},
    });
    expect(out).toEqual([]);
  });

  it("rejects when resource.id is missing", () => {
    const out = parse("session.resource.created", {
      type: "session.resource.created",
      resource: { type: "terminal", session_id: "conv_abc", name: "bash:s1", metadata: {} },
    });
    expect(out).toEqual([]);
  });

  it("rejects when resource.type is missing", () => {
    const out = parse("session.resource.created", {
      type: "session.resource.created",
      resource: { id: "terminal_bash_s1", session_id: "conv_abc", name: "bash:s1", metadata: {} },
    });
    expect(out).toEqual([]);
  });

  it("rejects when resource.session_id is missing", () => {
    // Without the owning session id the resource can't be routed to a
    // terminal cache, so the parse must reject it rather than emit a
    // resource that applyTerminalCreated would silently drop.
    const out = parse("session.resource.created", {
      type: "session.resource.created",
      resource: { id: "terminal_bash_s1", type: "terminal", name: "bash:s1", metadata: {} },
    });
    expect(out).toEqual([]);
  });

  it("rejects when resource.metadata is missing", () => {
    const out = parse("session.resource.created", {
      type: "session.resource.created",
      resource: {
        id: "terminal_bash_s1",
        type: "terminal",
        session_id: "conv_abc",
        name: "bash:s1",
      },
    });
    expect(out).toEqual([]);
  });

  it("rejects when resource is an array (not an object)", () => {
    const out = parse("session.resource.created", {
      type: "session.resource.created",
      resource: ["not", "a", "record"],
    });
    expect(out).toEqual([]);
  });
});

describe("session.resource.deleted (FLAT envelope)", () => {
  it("lifts resource_id, resource_type, session_id", () => {
    const out = parse("session.resource.deleted", {
      type: "session.resource.deleted",
      resource_id: "terminal_bash_s1",
      resource_type: "terminal",
      session_id: "conv_abc",
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionResourceDeletedEvent;
    expect(ev.type).toBe("session_resource_deleted");
    expect(ev.resourceId).toBe("terminal_bash_s1");
    expect(ev.resourceType).toBe("terminal");
    expect(ev.sessionId).toBe("conv_abc");
  });

  it("rejects missing resource_id", () => {
    const out = parse("session.resource.deleted", {
      type: "session.resource.deleted",
      resource_type: "terminal",
      session_id: "conv_abc",
    });
    expect(out).toEqual([]);
  });

  it("rejects missing resource_type", () => {
    const out = parse("session.resource.deleted", {
      type: "session.resource.deleted",
      resource_id: "terminal_bash_s1",
      session_id: "conv_abc",
    });
    expect(out).toEqual([]);
  });

  it("rejects missing session_id", () => {
    const out = parse("session.resource.deleted", {
      type: "session.resource.deleted",
      resource_id: "terminal_bash_s1",
      resource_type: "terminal",
    });
    expect(out).toEqual([]);
  });

  it("rejects empty-string fields", () => {
    const out = parse("session.resource.deleted", {
      type: "session.resource.deleted",
      resource_id: "",
      resource_type: "terminal",
      session_id: "conv_abc",
    });
    expect(out).toEqual([]);
  });
});

describe("response.heartbeat", () => {
  it("is a known no-op (does not yield an event, but is also not unknown)", () => {
    // Smoke: the parser yields nothing for heartbeat. The value of
    // having an explicit case is documentation + a single tripwire
    // location if the server ever attaches a payload.
    const out = parse("response.heartbeat", { type: "response.heartbeat" });
    expect(out).toEqual([]);
  });
});

describe("response.output_item.done (message)", () => {
  it("drops meta messages", () => {
    const out = parse("response.output_item.done", {
      type: "response.output_item.done",
      item: {
        id: "msg_meta",
        type: "message",
        status: "completed",
        response_id: "resp_skill",
        role: "user",
        is_meta: true,
        content: [{ type: "input_text", text: "<skill>hidden</skill>" }],
      },
    });
    expect(out).toEqual([]);
  });
});

describe("response.output_item.done (error)", () => {
  it("lifts persisted error items for live transcript rendering", () => {
    const out = parse("response.output_item.done", {
      type: "response.output_item.done",
      item: {
        id: "err_kiro",
        type: "error",
        status: "completed",
        response_id: "resp_kiro_failed_input",
        source: "execution",
        code: "kiro_native_prompt_not_recorded",
        message: "Kiro did not accept this web message.",
      },
    });

    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({
      type: "error",
      source: "execution",
      toolName: null,
      error: {
        code: "kiro_native_prompt_not_recorded",
        message: "Kiro did not accept this web message.",
      },
      itemId: "err_kiro",
      responseId: "resp_kiro_failed_input",
    });
  });
});

describe("response.output_item.done (slash_command)", () => {
  // parseOutputItem returns null for unknown item.types; without
  // these cases the live UI silently drops every Skill invocation.
  it("lifts a skill invocation with empty args + no stdout", () => {
    const out = parse("response.output_item.done", {
      type: "response.output_item.done",
      item: {
        id: "sc_1",
        type: "slash_command",
        status: "completed",
        response_id: "resp_slash",
        model: "claude-native-ui",
        kind: "skill",
        name: "dev-productivity:simplify",
        arguments: "",
        created_by: "alice@example.com",
      },
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SlashCommand;
    expect(ev.type).toBe("slash_command");
    expect(ev.kind).toBe("skill");
    expect(ev.name).toBe("dev-productivity:simplify");
    expect(ev.arguments).toBe("");
    // Server omits ``output`` via exclude_none; parser must coerce
    // to ``null`` so direct ``=== null`` checks downstream are sound.
    expect(ev.output).toBeNull();
    expect(ev.agentName).toBe("claude-native-ui");
    // Authorship lifts onto the event so the synthesized user-echo
    // bubble can carry the shared-session author label.
    expect(ev.createdBy).toBe("alice@example.com");
    expect(ev.itemId).toBe("sc_1");
    expect(ev.responseId).toBe("resp_slash");
  });

  it("lifts a built-in style record carrying inline stdout", () => {
    const out = parse("response.output_item.done", {
      type: "response.output_item.done",
      item: {
        id: "sc_2",
        type: "slash_command",
        status: "completed",
        response_id: "resp_slash",
        model: "claude-native-ui",
        kind: "skill",
        name: "oncall",
        arguments: "file-bug",
        output: "oncall: file-bug subcommand started",
      },
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SlashCommand;
    expect(ev.arguments).toBe("file-bug");
    expect(ev.output).toBe("oncall: file-bug subcommand started");
    // No created_by on the wire (agent/system receipt) → no author
    // invented on the event.
    expect(ev.createdBy).toBeUndefined();
  });

  it("lifts kind='command' for surfaced CLI built-ins", () => {
    const out = parse("response.output_item.done", {
      type: "response.output_item.done",
      item: {
        id: "sc_cmd",
        type: "slash_command",
        status: "completed",
        response_id: "resp_cmd",
        model: "claude-native-ui",
        kind: "command",
        name: "effort",
        arguments: "high",
      },
    });
    const ev = out[0] as SlashCommand;
    expect(ev.kind).toBe("command");
    expect(ev.name).toBe("effort");
  });

  it("defaults kind to 'skill' when the field is absent (back-compat)", () => {
    // Replaying a session captured before the bridge emitted ``kind``
    // must still render as a Skill card, not throw on missing prop.
    const out = parse("response.output_item.done", {
      type: "response.output_item.done",
      item: {
        id: "sc_legacy",
        type: "slash_command",
        status: "completed",
        response_id: "resp_legacy",
        model: "claude-native-ui",
        name: "oncall",
        arguments: "",
      },
    });
    const ev = out[0] as SlashCommand;
    expect(ev.kind).toBe("skill");
  });
});

describe("response.output_item.done (routing_decision)", () => {
  // parseOutputItem returns null for unknown item.types; without this
  // case the live UI silently drops the intelligent-model-router chip.
  it("lifts an applied routing decision with its verdict fields + id", () => {
    const out = parse("response.output_item.done", {
      type: "response.output_item.done",
      item: {
        id: "rd_1",
        type: "routing_decision",
        status: "completed",
        response_id: "routing_1",
        model: "databricks-claude-opus-4-8",
        applied: true,
        rationale: "multi-file refactor needs deep reasoning",
      },
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as RoutingDecision;
    expect(ev.type).toBe("routing_decision");
    expect(ev.model).toBe("databricks-claude-opus-4-8");
    expect(ev.applied).toBe(true);
    expect(ev.rationale).toBe("multi-file refactor needs deep reasoning");
    // The id (server re-publishes the persisted id) threads onto the event
    // so live and snapshot copies dedup by itemId — no double chip.
    expect(ev.itemId).toBe("rd_1");
    expect(ev.responseId).toBe("routing_1");
  });

  it("carries applied=false for a shadow verdict", () => {
    const out = parse("response.output_item.done", {
      type: "response.output_item.done",
      item: {
        id: "rd_shadow",
        type: "routing_decision",
        status: "completed",
        response_id: "routing_2",
        model: "databricks-claude-haiku-4-5",
        applied: false,
        rationale: "trivial",
      },
    });
    const ev = out[0] as RoutingDecision;
    expect(ev.applied).toBe(false);
  });

  it("drops a malformed routing decision (empty model)", () => {
    // A bad frame must not render a chip with no model — the parser drops
    // it so the live UI shows nothing rather than a broken chip.
    const out = parse("response.output_item.done", {
      type: "response.output_item.done",
      item: {
        id: "rd_bad",
        type: "routing_decision",
        status: "completed",
        response_id: "routing_3",
        model: "",
        applied: true,
        rationale: "x",
      },
    });
    expect(out).toEqual([]);
  });
});

describe("response.elicitation_request (FLAT envelope)", () => {
  it("lifts structured Codex requestUserInput payloads", () => {
    // Codex's final plan-mode prompt rides as the same
    // ``ask_user_question`` extra as Claude's AskUserQuestion flow.
    // If this parser drops the extra, ApprovalCard falls back to a
    // generic binary approval and the "Implement this plan?" options
    // never render.
    const out = parse("response.elicitation_request", {
      type: "response.elicitation_request",
      elicitation_id: "elicit_plan",
      params: {
        mode: "form",
        message: "Codex needs input",
        phase: "codex_request_user_input",
        policy_name: "codex_native_request_user_input",
        content_preview: "",
        requestedSchema: {},
        ask_user_question: {
          questions: [
            {
              id: "plan_decision",
              question: "Implement this plan?",
              options: [{ label: "Yes, implement this plan" }],
              multiSelect: false,
            },
          ],
        },
      },
    });

    expect(out).toHaveLength(1);
    const ev = out[0] as ElicitationRequest;
    expect(ev.type).toBe("elicitation_request");
    expect(ev.elicitationId).toBe("elicit_plan");
    expect(ev.message).toBe("Codex needs input");
    expect(ev.phase).toBe("codex_request_user_input");
    expect(ev.policyName).toBe("codex_native_request_user_input");
    expect(ev.askUserQuestion).toEqual({
      questions: [
        {
          id: "plan_decision",
          question: "Implement this plan?",
          options: [{ label: "Yes, implement this plan" }],
          multiSelect: false,
        },
      ],
    });
  });

  it("lifts structured Codex command approval details", () => {
    // Codex command approvals include a full JSON-RPC request in
    // ``content_preview`` for debugging, but the card should render
    // the structured extras instead. Dropping this payload makes the
    // web UI dump ids like threadId / itemId into the approval card.
    const out = parse("response.elicitation_request", {
      type: "response.elicitation_request",
      elicitation_id: "elicit_cmd",
      params: {
        mode: "form",
        message: "Codex wants to run **date**",
        phase: "codex_command_approval",
        policy_name: "codex_native_command_approval",
        content_preview: '{"threadId":"thread_123","command":"date"}',
        requestedSchema: {},
        command: "date",
        cwd: "/tmp/workspace",
        reason: "Run a focused test",
        execpolicy_amendment: [".venv/bin/python", "-m", "pytest"],
      },
    });

    expect(out).toHaveLength(1);
    const ev = out[0] as ElicitationRequest;
    expect(ev.type).toBe("elicitation_request");
    expect(ev.elicitationId).toBe("elicit_cmd");
    expect(ev.codexCommand).toEqual({
      command: "date",
      cwd: "/tmp/workspace",
      reason: "Run a focused test",
      execPolicyAmendment: [".venv/bin/python", "-m", "pytest"],
    });
  });

  it("lifts mirrored child approval target session id", () => {
    // Child/sub-agent prompts are mirrored into the parent stream.
    // The card must preserve the child id so approval posts to the
    // parked harness Future's owning session, not the currently open
    // parent chat.
    const out = parse("response.elicitation_request", {
      type: "response.elicitation_request",
      elicitation_id: "elicit_child_cmd",
      params: {
        mode: "form",
        message: "Codex wants to run **date**",
        phase: "codex_command_approval",
        policy_name: "codex_native_command_approval",
        content_preview: "{}",
        requestedSchema: {},
        target_session_id: "conv_child_123",
      },
    });

    expect(out).toHaveLength(1);
    const ev = out[0] as ElicitationRequest;
    expect(ev.type).toBe("elicitation_request");
    expect(ev.elicitationId).toBe("elicit_child_cmd");
    expect(ev.targetSessionId).toBe("conv_child_123");
  });

  it("lifts the allow_all_edits hint for claude-native edit-tool prompts", () => {
    // The server stamps ``allow_all_edits`` on edit-tool
    // PermissionRequests so the card can offer "Accept & allow all
    // edits". If this parser dropped it, the button would never
    // render even when the server intended it.
    const out = parse("response.elicitation_request", {
      type: "response.elicitation_request",
      elicitation_id: "elicit_edit",
      params: {
        mode: "form",
        message: "Claude wants to call **Edit**",
        phase: "pre_tool_use",
        policy_name: "claude_native_permission",
        content_preview: "Edit({})",
        requestedSchema: {},
        tool_name: "Edit",
        allow_all_edits: true,
      },
    });

    expect(out).toHaveLength(1);
    const ev = out[0] as ElicitationRequest;
    expect(ev.type).toBe("elicitation_request");
    expect(ev.allowAllEdits).toBe(true);
  });

  it("leaves allowAllEdits false when the hint is absent", () => {
    // Non-edit / non-claude-native prompts (here a Bash prompt) carry
    // no hint. ``allowAllEdits`` must stay false so the button is
    // gated off — switching to acceptEdits would be a no-op for Bash.
    const out = parse("response.elicitation_request", {
      type: "response.elicitation_request",
      elicitation_id: "elicit_bash",
      params: {
        mode: "form",
        message: "Claude wants to call **Bash**",
        phase: "pre_tool_use",
        policy_name: "claude_native_permission",
        content_preview: "Bash({})",
        requestedSchema: {},
        tool_name: "Bash",
      },
    });

    expect(out).toHaveLength(1);
    const ev = out[0] as ElicitationRequest;
    expect(ev.allowAllEdits).toBe(false);
  });

  it("lifts the remember_scope hint with a host for WebFetch prompts", () => {
    // The server stamps ``remember_scope`` on non-edit tool
    // PermissionRequests so the card can offer "Approve & don't ask
    // again for <host>". For WebFetch the host scopes the rule, so it
    // must survive parsing.
    const out = parse("response.elicitation_request", {
      type: "response.elicitation_request",
      elicitation_id: "elicit_webfetch",
      params: {
        mode: "form",
        message: "Claude wants to call **WebFetch**",
        phase: "pre_tool_use",
        policy_name: "claude_native_permission",
        content_preview: 'WebFetch({"url": "https://github.com/a/b"})',
        requestedSchema: {},
        tool_name: "WebFetch",
        remember_scope: { tool: "WebFetch", host: "github.com" },
      },
    });

    expect(out).toHaveLength(1);
    const ev = out[0] as ElicitationRequest;
    expect(ev.rememberScope).toEqual({ tool: "WebFetch", host: "github.com" });
  });

  it("lifts a tool-wide remember_scope hint (no host)", () => {
    // Non-WebFetch tools (here Bash) get a tool-wide scope: ``tool``
    // only, no ``host``. The card labels the button by the tool name.
    const out = parse("response.elicitation_request", {
      type: "response.elicitation_request",
      elicitation_id: "elicit_bash_remember",
      params: {
        mode: "form",
        message: "Claude wants to call **Bash**",
        phase: "pre_tool_use",
        policy_name: "claude_native_permission",
        content_preview: "Bash({})",
        requestedSchema: {},
        tool_name: "Bash",
        remember_scope: { tool: "Bash" },
      },
    });

    expect(out).toHaveLength(1);
    const ev = out[0] as ElicitationRequest;
    expect(ev.rememberScope).toEqual({ tool: "Bash", host: undefined });
  });

  it("leaves rememberScope null when the hint is absent", () => {
    // Edit tools / ExitPlanMode / AskUserQuestion carry no
    // ``remember_scope``; the button must stay hidden.
    const out = parse("response.elicitation_request", {
      type: "response.elicitation_request",
      elicitation_id: "elicit_edit_no_remember",
      params: {
        mode: "form",
        message: "Claude wants to call **Edit**",
        phase: "pre_tool_use",
        policy_name: "claude_native_permission",
        content_preview: "Edit({})",
        requestedSchema: {},
        tool_name: "Edit",
        allow_all_edits: true,
      },
    });

    expect(out).toHaveLength(1);
    const ev = out[0] as ElicitationRequest;
    expect(ev.rememberScope).toBeNull();
  });
});

describe("response.elicitation_resolved (FLAT envelope)", () => {
  it("lifts elicitation_id and emits a single elicitation_resolved", () => {
    // The server publishes this event from every approval-clearing
    // path (UI approval dispatch, PermissionRequest hook finally,
    // tool-result auto-resolve). The chat-store matches the
    // resolved ApprovalCard by elicitationId — if the parser drops
    // the id or misnames it, the handler silently fails to clear
    // the card and the user sees a stuck pending prompt.
    const out = parse("response.elicitation_resolved", {
      type: "response.elicitation_resolved",
      elicitation_id: "elicit_abc",
    });
    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({
      type: "elicitation_resolved",
      elicitationId: "elicit_abc",
    });
  });

  it("rejects missing elicitation_id", () => {
    // An event without the correlation id is unmatchable on the
    // client — skip it rather than handing the handler a sentinel
    // that would clear the wrong card (or no card with confusing
    // semantics).
    const out = parse("response.elicitation_resolved", {
      type: "response.elicitation_resolved",
    });
    expect(out).toEqual([]);
  });

  it("rejects empty-string elicitation_id", () => {
    // Defensive — an empty string would compare equal to a
    // similarly malformed cached id; the type guard rejects it
    // alongside missing-key for consistency.
    const out = parse("response.elicitation_resolved", {
      type: "response.elicitation_resolved",
      elicitation_id: "",
    });
    expect(out).toEqual([]);
  });
});

describe("session.usage (FLAT envelope)", () => {
  it("lifts conversation_id and context_tokens", () => {
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      context_tokens: 44568,
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionUsageEvent;
    expect(ev.type).toBe("session_usage");
    expect(ev.conversationId).toBe("conv_abc");
    expect(ev.contextTokens).toBe(44568);
    expect(ev.contextWindow).toBeUndefined();
  });

  it("lifts context_window when present alongside context_tokens", () => {
    // The forwarder pushes both fields the first time it learns the
    // user's Claude model — the ring needs them paired to render the
    // correct ratio for opus[1m] / sonnet[1m] tiers.
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      context_tokens: 250000,
      context_window: 1000000,
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionUsageEvent;
    expect(ev.contextTokens).toBe(250000);
    expect(ev.contextWindow).toBe(1000000);
  });

  it("accepts window-only broadcasts (no context_tokens)", () => {
    // The forwarder may send just context_window when the user
    // switches models without producing a new message.usage block.
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      context_window: 1000000,
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionUsageEvent;
    expect(ev.contextTokens).toBeUndefined();
    expect(ev.contextWindow).toBe(1000000);
  });

  it("rejects payloads with neither field", () => {
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
    });
    expect(out).toEqual([]);
  });

  it("rejects negative context_tokens", () => {
    // The context ring computes `tokensUsed / contextWindow`; a negative
    // numerator would silently misreport context occupancy as 0%.
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      context_tokens: -1,
    });
    expect(out).toEqual([]);
  });

  it("rejects non-numeric context_tokens", () => {
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      context_tokens: "44568",
    });
    expect(out).toEqual([]);
  });

  it("rejects non-positive context_window", () => {
    // contextWindow is the ring denominator; zero would NaN the math.
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      context_window: 0,
    });
    expect(out).toEqual([]);
  });

  it("accepts cost-only broadcasts (relay path, no context fields)", () => {
    // The Omnigent relay path emits a session.usage carrying only the
    // cumulative cost — context_tokens/window ride on response.completed.
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      total_cost_usd: 0.42,
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionUsageEvent;
    expect(ev.totalCostUsd).toBe(0.42);
    expect(ev.contextTokens).toBeUndefined();
    expect(ev.contextWindow).toBeUndefined();
  });

  it("lifts total_cost_usd alongside context fields (native path)", () => {
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      context_tokens: 44568,
      context_window: 200000,
      total_cost_usd: 1.25,
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionUsageEvent;
    expect(ev.contextTokens).toBe(44568);
    expect(ev.contextWindow).toBe(200000);
    expect(ev.totalCostUsd).toBe(1.25);
  });

  it("accepts a priced zero cost ($0.00 is distinct from unpriced)", () => {
    // A priced session whose spend is exactly 0.0 still carries the
    // field — the client shows $0.00, not the unpriced "—".
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      total_cost_usd: 0,
    });
    expect(out).toHaveLength(1);
    expect((out[0] as SessionUsageEvent).totalCostUsd).toBe(0);
  });

  it("rejects negative total_cost_usd", () => {
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      total_cost_usd: -0.5,
    });
    expect(out).toEqual([]);
  });

  it("rejects non-numeric total_cost_usd", () => {
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      total_cost_usd: "0.42",
    });
    expect(out).toEqual([]);
  });

  it("lifts usage_by_model into a per-model map with missing buckets as null", () => {
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      total_cost_usd: 0.42,
      usage_by_model: {
        "model-a": { input_tokens: 1000, output_tokens: 500, total_cost_usd: 0.4 },
        "model-b": { input_tokens: 200 },
      },
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionUsageEvent;
    expect(ev.usageByModel).toBeDefined();
    // Present buckets are lifted; absent buckets become null (not omitted),
    // so the UI can distinguish "not recorded" from a real zero.
    expect(ev.usageByModel!["model-a"].inputTokens).toBe(1000);
    expect(ev.usageByModel!["model-a"].outputTokens).toBe(500);
    expect(ev.usageByModel!["model-a"].totalCostUsd).toBe(0.4);
    expect(ev.usageByModel!["model-b"].inputTokens).toBe(200);
    expect(ev.usageByModel!["model-b"].totalCostUsd).toBeNull();
  });

  it("accepts a usage_by_model-only broadcast (no flat fields)", () => {
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      usage_by_model: { "model-a": { input_tokens: 1000 } },
    });
    expect(out).toHaveLength(1);
    expect((out[0] as SessionUsageEvent).usageByModel!["model-a"].inputTokens).toBe(1000);
  });

  it("rejects usage_by_model with a malformed (negative) bucket", () => {
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      usage_by_model: { "model-a": { input_tokens: -5 } },
    });
    expect(out).toEqual([]);
  });

  it("rejects usage_by_model whose entry is not an object", () => {
    const out = parse("session.usage", {
      type: "session.usage",
      conversation_id: "conv_abc",
      usage_by_model: { "model-a": 1000 },
    });
    expect(out).toEqual([]);
  });
});

describe("session.todos (FLAT envelope)", () => {
  it("lifts conversation_id and todos with all three status values", () => {
    const out = parse("session.todos", {
      type: "session.todos",
      conversation_id: "conv_abc",
      todos: [
        { content: "Write tests", status: "completed", activeForm: "Writing tests" },
        { content: "Fix the bug", status: "in_progress", activeForm: "Fixing the bug" },
        { content: "Review PR", status: "pending", activeForm: "Reviewing PR" },
      ],
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionTodosEvent;
    expect(ev.type).toBe("session_todos");
    expect(ev.conversationId).toBe("conv_abc");
    expect(ev.todos).toHaveLength(3);
    expect(ev.todos[0]).toEqual({
      content: "Write tests",
      status: "completed",
      activeForm: "Writing tests",
    });
    expect(ev.todos[1]).toEqual({
      content: "Fix the bug",
      status: "in_progress",
      activeForm: "Fixing the bug",
    });
    expect(ev.todos[2]).toEqual({
      content: "Review PR",
      status: "pending",
      activeForm: "Reviewing PR",
    });
  });

  it("accepts an empty todos array", () => {
    // The panel renders nothing when todos is empty, but the event itself
    // is valid — a completed run legitimately ends with an empty list.
    const out = parse("session.todos", {
      type: "session.todos",
      conversation_id: "conv_abc",
      todos: [],
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionTodosEvent;
    expect(ev.todos).toEqual([]);
  });

  it("silently drops items with non-string activeForm, keeps the rest", () => {
    // activeForm must be a string (the gerund form, e.g. "Running tests").
    // Booleans, null, and missing values are rejected.
    const out = parse("session.todos", {
      type: "session.todos",
      conversation_id: "conv_abc",
      todos: [
        { content: "Valid item", status: "pending", activeForm: "Doing the thing" },
        { content: "Boolean activeForm", status: "pending", activeForm: false },
        { content: "Null activeForm", status: "pending", activeForm: null },
      ],
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionTodosEvent;
    // Only the string-activeForm item survives.
    expect(ev.todos).toHaveLength(1);
    expect(ev.todos[0].content).toBe("Valid item");
  });

  it("silently drops items with an invalid status value", () => {
    const out = parse("session.todos", {
      type: "session.todos",
      conversation_id: "conv_abc",
      todos: [
        { content: "Valid", status: "pending", activeForm: "Doing valid" },
        { content: "Invalid status", status: "done", activeForm: "Doing invalid" },
        { content: "Also invalid", status: 42, activeForm: "Also doing invalid" },
      ],
    });
    const ev = out[0] as SessionTodosEvent;
    // "done" and numeric status are not in the allowed union; only "pending" survives.
    expect(ev.todos).toHaveLength(1);
    expect(ev.todos[0].content).toBe("Valid");
  });

  it("silently drops items with missing or non-string content", () => {
    const out = parse("session.todos", {
      type: "session.todos",
      conversation_id: "conv_abc",
      todos: [
        { content: "OK", status: "pending", activeForm: "Doing OK" },
        { status: "pending", activeForm: "Missing content" },
        { content: 42, status: "pending", activeForm: "Numeric content" },
      ],
    });
    const ev = out[0] as SessionTodosEvent;
    // Items without a string content field are dropped; others pass through.
    expect(ev.todos).toHaveLength(1);
    expect(ev.todos[0].content).toBe("OK");
  });

  it("returns empty todos array (not null) when all items are malformed", () => {
    // Ensures the panel renders the empty state rather than crashing on null.
    const out = parse("session.todos", {
      type: "session.todos",
      conversation_id: "conv_abc",
      todos: [
        { content: "No activeForm field", status: "pending" },
        { content: "Boolean activeForm", status: "pending", activeForm: false },
      ],
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionTodosEvent;
    expect(ev.todos).toEqual([]);
  });

  it("rejects missing conversation_id", () => {
    const out = parse("session.todos", {
      type: "session.todos",
      todos: [{ content: "A task", status: "pending", activeForm: "Doing a task" }],
    });
    expect(out).toEqual([]);
  });

  it("rejects when todos field is not an array", () => {
    const out = parse("session.todos", {
      type: "session.todos",
      conversation_id: "conv_abc",
      todos: null,
    });
    expect(out).toEqual([]);
  });
});

describe("session.terminal_pending (FLAT envelope)", () => {
  it("lifts conversation_id and pending=true", () => {
    const out = parse("session.terminal_pending", {
      type: "session.terminal_pending",
      conversation_id: "conv_abc",
      pending: true,
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionTerminalPendingEvent;
    expect(ev.type).toBe("session_terminal_pending");
    expect(ev.conversationId).toBe("conv_abc");
    expect(ev.pending).toBe(true);
  });

  it("lifts pending=false (clear)", () => {
    const out = parse("session.terminal_pending", {
      type: "session.terminal_pending",
      conversation_id: "conv_abc",
      pending: false,
    });
    expect(out).toHaveLength(1);
    expect((out[0] as SessionTerminalPendingEvent).pending).toBe(false);
  });

  it("coerces a missing/non-boolean pending to false rather than dropping the frame", () => {
    // A malformed `pending` must not strand the spinner on; the safe
    // default is "not pending".
    const out = parse("session.terminal_pending", {
      type: "session.terminal_pending",
      conversation_id: "conv_abc",
    });
    expect(out).toHaveLength(1);
    expect((out[0] as SessionTerminalPendingEvent).pending).toBe(false);
  });

  it("rejects missing conversation_id", () => {
    const out = parse("session.terminal_pending", {
      type: "session.terminal_pending",
      pending: true,
    });
    expect(out).toEqual([]);
  });
});

describe("session.sandbox_status (FLAT envelope)", () => {
  it("lifts conversation_id, stage, and error", () => {
    const out = parse("session.sandbox_status", {
      type: "session.sandbox_status",
      conversation_id: "conv_abc",
      stage: "cloning",
      error: null,
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionSandboxStatusEvent;
    expect(ev.type).toBe("session_sandbox_status");
    expect(ev.conversationId).toBe("conv_abc");
    expect(ev.stage).toBe("cloning");
    expect(ev.error).toBeNull();
  });

  it("carries the failure reason on stage=failed", () => {
    const out = parse("session.sandbox_status", {
      type: "session.sandbox_status",
      conversation_id: "conv_abc",
      stage: "failed",
      error: "managed sandbox launch failed: spend limit reached",
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionSandboxStatusEvent;
    expect(ev.stage).toBe("failed");
    expect(ev.error).toBe("managed sandbox launch failed: spend limit reached");
  });

  it("drops frames with an unknown stage", () => {
    // A bogus stage must not render a bogus step — the snapshot
    // re-seeds the indicator on the next load.
    const out = parse("session.sandbox_status", {
      type: "session.sandbox_status",
      conversation_id: "conv_abc",
      stage: "warming-up",
    });
    expect(out).toEqual([]);
  });

  it("rejects missing conversation_id", () => {
    const out = parse("session.sandbox_status", {
      type: "session.sandbox_status",
      stage: "provisioning",
    });
    expect(out).toEqual([]);
  });
});

describe("session.model (FLAT envelope)", () => {
  it("lifts conversation_id and model alias", () => {
    const events = parse("session.model", { conversation_id: "conv_abc", model: "opus" });
    expect(events).toHaveLength(1);
    const ev = events[0] as SessionModelEvent;
    expect(ev.type).toBe("session_model");
    expect(ev.conversationId).toBe("conv_abc");
    expect(ev.model).toBe("opus");
  });

  it("rejects missing model", () => {
    expect(parse("session.model", { conversation_id: "conv_abc" })).toEqual([]);
  });

  it("rejects missing conversation_id", () => {
    expect(parse("session.model", { model: "opus" })).toEqual([]);
  });
});

describe("session.reasoning_effort (FLAT envelope)", () => {
  it("lifts conversation_id and effort", () => {
    const events = parse("session.reasoning_effort", {
      conversation_id: "conv_abc",
      reasoning_effort: "medium",
    });
    expect(events).toHaveLength(1);
    const ev = events[0] as SessionReasoningEffortEvent;
    expect(ev.type).toBe("session_reasoning_effort");
    expect(ev.conversationId).toBe("conv_abc");
    expect(ev.reasoningEffort).toBe("medium");
  });

  it("lifts null effort clears", () => {
    const events = parse("session.reasoning_effort", {
      conversation_id: "conv_abc",
      reasoning_effort: null,
    });
    expect(events).toHaveLength(1);
    const ev = events[0] as SessionReasoningEffortEvent;
    expect(ev.reasoningEffort).toBeNull();
  });

  it("rejects missing effort", () => {
    expect(parse("session.reasoning_effort", { conversation_id: "conv_abc" })).toEqual([]);
  });
});

describe("session.collaboration_mode (FLAT envelope)", () => {
  it("lifts conversation_id and mode string", () => {
    const events = parse("session.collaboration_mode", {
      conversation_id: "conv_abc",
      mode: "plan",
    });
    expect(events).toHaveLength(1);
    const ev = events[0] as SessionCollaborationModeEvent;
    expect(ev.type).toBe("session_collaboration_mode");
    expect(ev.conversationId).toBe("conv_abc");
    expect(ev.mode).toBe("plan");
  });

  it("rejects missing mode", () => {
    expect(parse("session.collaboration_mode", { conversation_id: "conv_abc" })).toEqual([]);
  });

  it("rejects missing conversation_id", () => {
    expect(parse("session.collaboration_mode", { mode: "plan" })).toEqual([]);
  });
});

describe("session.agent_changed (FLAT envelope)", () => {
  it("lifts conversation_id, agent_id, and agent_name", () => {
    const events = parse("session.agent_changed", {
      type: "session.agent_changed",
      conversation_id: "conv_abc",
      agent_id: "ag_clone1",
      agent_name: "Claude Code (switch ag_clone1)",
    });
    expect(events).toHaveLength(1);
    const ev = events[0] as SessionAgentChangedEvent;
    expect(ev.type).toBe("session_agent_changed");
    expect(ev.conversationId).toBe("conv_abc");
    expect(ev.agentId).toBe("ag_clone1");
    expect(ev.agentName).toBe("Claude Code (switch ag_clone1)");
  });

  it("rejects missing agent_id", () => {
    expect(
      parse("session.agent_changed", { conversation_id: "conv_abc", agent_name: "x" }),
    ).toEqual([]);
  });

  it("rejects missing agent_name", () => {
    expect(
      parse("session.agent_changed", { conversation_id: "conv_abc", agent_id: "ag_1" }),
    ).toEqual([]);
  });

  it("rejects missing conversation_id", () => {
    expect(parse("session.agent_changed", { agent_id: "ag_1", agent_name: "x" })).toEqual([]);
  });
});

describe("session.child_session.updated (FLAT envelope)", () => {
  it("lifts parent/child ids and the child summary", () => {
    const out = parse("session.child_session.updated", {
      type: "session.child_session.updated",
      conversation_id: "conv_parent",
      child_session_id: "conv_child1",
      child: {
        id: "conv_child1",
        title: "researcher:auth",
        tool: "researcher",
        session_name: "auth",
        current_task_status: "in_progress",
        busy: true,
        last_message_preview: "looking…",
      },
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionChildSessionUpdatedEvent;
    expect(ev.type).toBe("session_child_session_updated");
    expect(ev.conversationId).toBe("conv_parent");
    expect(ev.childSessionId).toBe("conv_child1");
    expect(ev.child.busy).toBe(true);
    expect(ev.child.current_task_status).toBe("in_progress");
  });

  it("rejects when child is absent or not an object", () => {
    expect(
      parse("session.child_session.updated", {
        type: "session.child_session.updated",
        conversation_id: "conv_parent",
        child_session_id: "conv_child1",
      }),
    ).toEqual([]);
    expect(
      parse("session.child_session.updated", {
        type: "session.child_session.updated",
        conversation_id: "conv_parent",
        child_session_id: "conv_child1",
        child: [],
      }),
    ).toEqual([]);
  });
});

describe("session.presence (FLAT envelope)", () => {
  it("lifts the full viewer list with idle flags", () => {
    const out = parse("session.presence", {
      type: "session.presence",
      conversation_id: "conv_abc",
      viewers: [
        { user_id: "alice@example.com", joined_at: "2026-06-10T17:00:00Z", idle: false },
        { user_id: "bob@example.com", joined_at: "2026-06-10T17:02:11Z", idle: true },
      ],
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionPresenceEvent;
    expect(ev.type).toBe("session_presence");
    expect(ev.conversationId).toBe("conv_abc");
    // Content, not just shape: both identities and their idle flags
    // must survive the snake_case → camelCase boundary, or the header
    // renders the wrong people (or greys the wrong one).
    expect(ev.viewers).toEqual([
      { userId: "alice@example.com", joinedAt: "2026-06-10T17:00:00Z", idle: false },
      { userId: "bob@example.com", joinedAt: "2026-06-10T17:02:11Z", idle: true },
    ]);
  });

  it("parses an empty viewer list (everyone left)", () => {
    const out = parse("session.presence", {
      type: "session.presence",
      conversation_id: "conv_abc",
      viewers: [],
    });
    expect(out).toHaveLength(1);
    // The full-state protocol clears the header by sending [], so this
    // must NOT be rejected as malformed.
    expect((out[0] as SessionPresenceEvent).viewers).toEqual([]);
  });

  it("defaults a missing idle flag to active and tolerates no joined_at", () => {
    const out = parse("session.presence", {
      type: "session.presence",
      conversation_id: "conv_abc",
      viewers: [{ user_id: "alice@example.com" }],
    });
    expect(out).toHaveLength(1);
    expect((out[0] as SessionPresenceEvent).viewers).toEqual([
      { userId: "alice@example.com", idle: false },
    ]);
  });

  it("rejects a viewer entry without a user_id", () => {
    expect(
      parse("session.presence", {
        type: "session.presence",
        conversation_id: "conv_abc",
        viewers: [{ idle: true }],
      }),
    ).toEqual([]);
  });

  it("rejects missing conversation_id or non-array viewers", () => {
    expect(
      parse("session.presence", {
        type: "session.presence",
        viewers: [],
      }),
    ).toEqual([]);
    expect(
      parse("session.presence", {
        type: "session.presence",
        conversation_id: "conv_abc",
        viewers: "nobody",
      }),
    ).toEqual([]);
  });
});

describe("session.changed_files.invalidated (FLAT envelope)", () => {
  it("lifts session id and defaults environment", () => {
    const out = parse("session.changed_files.invalidated", {
      type: "session.changed_files.invalidated",
      session_id: "conv_abc",
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionChangedFilesInvalidatedEvent;
    expect(ev.type).toBe("session_changed_files_invalidated");
    expect(ev.sessionId).toBe("conv_abc");
    expect(ev.environmentId).toBe("default");
  });

  it("rejects when session_id is absent", () => {
    expect(
      parse("session.changed_files.invalidated", {
        type: "session.changed_files.invalidated",
      }),
    ).toEqual([]);
  });
});

describe("session.terminal.activity (FLAT envelope)", () => {
  it("lifts session and terminal ids", () => {
    const out = parse("session.terminal.activity", {
      type: "session.terminal.activity",
      session_id: "conv_abc",
      terminal_id: "terminal_zsh_s1",
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionTerminalActivityEvent;
    expect(ev.type).toBe("session_terminal_activity");
    expect(ev.sessionId).toBe("conv_abc");
    expect(ev.terminalId).toBe("terminal_zsh_s1");
  });

  it("rejects when terminal_id is absent", () => {
    expect(
      parse("session.terminal.activity", {
        type: "session.terminal.activity",
        session_id: "conv_abc",
      }),
    ).toEqual([]);
  });
});

describe("session.skills (FLAT envelope)", () => {
  it("lifts conversation_id into the bare nudge", () => {
    const out = parse("session.skills", {
      type: "session.skills",
      conversation_id: "conv_abc",
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionSkillsEvent;
    expect(ev.type).toBe("session_skills");
    expect(ev.conversationId).toBe("conv_abc");
  });

  it("rejects missing conversation_id", () => {
    // Without a conversation id the store handler can't target a
    // refetch, so the frame must be dropped rather than lifted.
    const out = parse("session.skills", {
      type: "session.skills",
    });
    expect(out).toEqual([]);
  });

  it("rejects an empty conversation_id", () => {
    const out = parse("session.skills", {
      type: "session.skills",
      conversation_id: "",
    });
    expect(out).toEqual([]);
  });
});

describe("session.model_options (FLAT envelope)", () => {
  it("lifts conversation_id into the bare nudge", () => {
    const out = parse("session.model_options", {
      type: "session.model_options",
      conversation_id: "conv_abc",
    });
    expect(out).toHaveLength(1);
    const ev = out[0] as SessionModelOptionsEvent;
    expect(ev.type).toBe("session_model_options");
    expect(ev.conversationId).toBe("conv_abc");
  });

  it("rejects missing conversation_id", () => {
    const out = parse("session.model_options", {
      type: "session.model_options",
    });
    expect(out).toEqual([]);
  });
});
