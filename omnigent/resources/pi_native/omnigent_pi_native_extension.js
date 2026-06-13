// Auto-generated Omnigent bridge extension for native Pi sessions.
const fs = require("fs");
const path = require("path");

function readConfig() {
  const configPath = process.env.OMNIGENT_PI_NATIVE_CONFIG;
  if (!configPath) return null;
  try {
    return JSON.parse(fs.readFileSync(configPath, "utf8"));
  } catch (_err) {
    return null;
  }
}

function textFromContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    const text =
      block.text || block.input_text || block.output_text || block.content;
    if (typeof text === "string") parts.push(text);
  }
  return parts.join("");
}

function textFromMessage(message) {
  if (!message || typeof message !== "object") return "";
  return textFromContent(
    message.content || message.parts || message.message || "",
  );
}

function safeJsonStringify(value) {
  try {
    return JSON.stringify(value ?? {});
  } catch (_err) {
    return String(value);
  }
}

function textFromToolResult(event) {
  if (!event || typeof event !== "object") return "";
  const text = textFromContent(event.content);
  if (text) return text;
  if ("result" in event) {
    const result = event.result;
    if (typeof result === "string") return result;
    if (result && typeof result === "object") {
      const resultText = textFromContent(result.content);
      if (resultText) return resultText;
    }
    return safeJsonStringify(result);
  }
  if ("details" in event) return safeJsonStringify(event.details);
  return "";
}

function contentBlocks(message) {
  if (
    !message ||
    typeof message !== "object" ||
    !Array.isArray(message.content)
  )
    return [];
  return message.content;
}

function fingerprint(text) {
  let hash = 5381;
  for (let i = 0; i < text.length; i += 1) {
    hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
  }
  return `${text.length}-${hash.toString(36)}`;
}

function messageRole(message) {
  if (!message || typeof message !== "object") return "";
  return String(message.role || message.type || "");
}

function headers(config) {
  return {
    "content-type": "application/json",
    ...(config.authHeaders || {}),
  };
}

async function postEvent(config, body) {
  if (
    !config ||
    !config.serverUrl ||
    !config.sessionId ||
    typeof fetch !== "function"
  )
    return;
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}/events`;
  try {
    await fetch(url, {
      method: "POST",
      headers: headers(config),
      body: JSON.stringify(body),
    });
  } catch (_err) {
    // Keep Pi responsive even if Omnigent is temporarily unavailable.
  }
}

async function patchExternalSessionId(config, nativeSessionId) {
  if (
    !nativeSessionId ||
    !config ||
    !config.serverUrl ||
    typeof fetch !== "function"
  )
    return;
  const url = `${config.serverUrl}/v1/sessions/${encodeURIComponent(config.sessionId)}`;
  try {
    await fetch(url, {
      method: "PATCH",
      headers: headers(config),
      body: JSON.stringify({ external_session_id: nativeSessionId }),
    });
  } catch (_err) {}
}

function setOmnigentStatus(config, ctx, state) {
  if (!ctx || !ctx.ui || !config) return;
  const urlLabel = config.conversationUrl
    ? `Omnigent: ${config.conversationUrl}`
    : "Omnigent";
  const label = state ? `${urlLabel} · ${state}` : urlLabel;
  try {
    ctx.ui.setTitle(`Omnigent: ${config.sessionId}`);
    ctx.ui.setStatus("omnigent", label);
    ctx.ui.setStatus("omnigent_state", undefined);
  } catch (_err) {}
}

function interruptActiveContext(ctx) {
  if (!ctx || typeof ctx.abort !== "function") return false;
  try {
    ctx.abort();
    return true;
  } catch (_err) {
    return false;
  }
}

function startInboxPoller(pi, config, handleInterrupt) {
  if (!config || !config.inboxDir || pi.__omnigentInboxPoller) return;
  const seen = new Set();
  pi.__omnigentInboxPoller = setInterval(() => {
    let files = [];
    try {
      files = fs
        .readdirSync(config.inboxDir)
        .filter((name) => name.endsWith(".json"))
        .sort();
    } catch (_err) {
      return;
    }
    for (const file of files) {
      const fullPath = path.join(config.inboxDir, file);
      let payload;
      try {
        payload = JSON.parse(fs.readFileSync(fullPath, "utf8"));
      } catch (_err) {
        continue;
      }
      if (!payload || seen.has(payload.id)) {
        try {
          fs.unlinkSync(fullPath);
        } catch (_err) {}
        continue;
      }
      seen.add(payload.id);
      if (
        payload.type === "user_message" &&
        typeof payload.content === "string"
      ) {
        try {
          pi.sendUserMessage(payload.content, { deliverAs: "followUp" });
        } catch (_err) {
          seen.delete(payload.id);
          continue;
        }
      }
      if (payload.type === "interrupt") {
        const accepted =
          typeof handleInterrupt === "function" ? handleInterrupt() : false;
        if (!accepted) {
          seen.delete(payload.id);
          continue;
        }
      }
      try {
        fs.unlinkSync(fullPath);
      } catch (_err) {}
    }
  }, 250);
}

module.exports = function (pi) {
  const config = readConfig();
  let sequence = 0;
  let turnOrdinal = 0;
  let activeResponseId = null;
  let latestContext = null;
  let pendingInterruptUntil = 0;
  const postedToolCalls = new Set();
  const postedToolResults = new Set();
  const postedReasoning = new Set();
  const toolCallsById = new Map();
  const pendingInterruptMs = 30_000;

  function rememberContext(ctx) {
    if (ctx) latestContext = ctx;
  }

  function newResponseId(prefix) {
    return `pi-${prefix}-${Date.now()}-${++sequence}`;
  }

  function currentResponseId() {
    if (!activeResponseId) activeResponseId = newResponseId("turn");
    return activeResponseId;
  }

  function hasPendingInterrupt() {
    if (!pendingInterruptUntil) return false;
    if (Date.now() > pendingInterruptUntil) {
      pendingInterruptUntil = 0;
      return false;
    }
    return true;
  }

  function requestInterrupt(ctx) {
    const accepted = interruptActiveContext(ctx);
    if (!accepted) return false;
    pendingInterruptUntil = Date.now() + pendingInterruptMs;
    return true;
  }

  function replayPendingInterrupt(ctx) {
    if (!hasPendingInterrupt()) return false;
    interruptActiveContext(ctx);
    return true;
  }

  function clearPendingInterrupt() {
    pendingInterruptUntil = 0;
  }

  async function postToolCall(toolCall, responseId) {
    if (!toolCall || typeof toolCall !== "object") return;
    const callId = String(toolCall.id || toolCall.toolCallId || "");
    const name = String(toolCall.name || toolCall.toolName || "");
    if (!callId || !name) return;
    const key = `${responseId}:${callId}`;
    toolCallsById.set(callId, { key, responseId, name });
    if (postedToolCalls.has(key)) return;
    postedToolCalls.add(key);
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: responseId,
        item_type: "function_call",
        item_data: {
          agent: "Pi",
          name,
          arguments: safeJsonStringify(
            toolCall.arguments ?? toolCall.input ?? {},
          ),
          call_id: callId,
        },
      },
    });
  }

  async function postToolResult(event, responseId) {
    if (!event || typeof event !== "object") return;
    const callId = String(event.toolCallId || event.id || "");
    if (!callId) return;
    const known = toolCallsById.get(callId);
    const key = known && known.key ? known.key : `${responseId}:${callId}`;
    if (postedToolResults.has(key)) return;
    postedToolResults.add(key);
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: known && known.responseId ? known.responseId : responseId,
        item_type: "function_call_output",
        item_data: {
          call_id: callId,
          output: textFromToolResult(event),
        },
      },
    });
  }

  async function postReasoningText(text, responseId, keyHint) {
    if (typeof text !== "string" || !text.trim()) return;
    const textKey = `${responseId}:text:${fingerprint(text)}`;
    const key = `${responseId}:${keyHint || fingerprint(text)}`;
    if (postedReasoning.has(key) || postedReasoning.has(textKey)) return;
    postedReasoning.add(key);
    postedReasoning.add(textKey);
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: responseId,
        item_type: "reasoning",
        item_data: {
          agent: "Pi",
          summary: [],
          content: [{ type: "reasoning_text", text }],
        },
      },
    });
  }

  async function mirrorAssistantMessage(message, responseId) {
    const blocks = contentBlocks(message);
    for (let index = 0; index < blocks.length; index += 1) {
      const block = blocks[index];
      if (!block || typeof block !== "object") continue;
      if (block.type === "toolCall") await postToolCall(block, responseId);
      if (block.type === "thinking") {
        const text = typeof block.thinking === "string" ? block.thinking : "";
        const key = block.thinkingSignature || `${turnOrdinal}:${index}`;
        await postReasoningText(text, responseId, key);
      }
    }
  }

  pi.registerCommand("omnigent", {
    description: "Show the Omnigent conversation URL",
    async handler(_args, ctx) {
      setOmnigentStatus(config, ctx, "linked");
      if (ctx && ctx.ui && config && config.conversationUrl) {
        ctx.ui.notify(`Omnigent: ${config.conversationUrl}`, "info");
      }
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    rememberContext(ctx);
    setOmnigentStatus(config, ctx, "linked");
    startInboxPoller(pi, config, () => requestInterrupt(latestContext));
    const nativeSessionId =
      ctx && ctx.sessionManager && ctx.sessionManager.getSessionId
        ? ctx.sessionManager.getSessionId()
        : undefined;
    await patchExternalSessionId(config, nativeSessionId);
    await postEvent(config, {
      type: "external_session_status",
      data: { status: "idle", response_id: `pi-${Date.now()}-${++sequence}` },
    });
  });

  pi.on("agent_start", async (_event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    setOmnigentStatus(config, ctx, "running");
    activeResponseId = null;
    turnOrdinal = 0;
    postedToolCalls.clear();
    postedToolResults.clear();
    postedReasoning.clear();
    toolCallsById.clear();
    await postEvent(config, {
      type: "external_session_status",
      data: {
        status: "running",
        response_id: `pi-${Date.now()}-${++sequence}`,
      },
    });
  });

  pi.on("agent_end", async (_event, ctx) => {
    rememberContext(ctx);
    clearPendingInterrupt();
    setOmnigentStatus(config, ctx, "idle");
    activeResponseId = null;
    await postEvent(config, {
      type: "external_session_status",
      data: { status: "idle", response_id: `pi-${Date.now()}-${++sequence}` },
    });
  });

  pi.on("turn_start", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const index =
      event && typeof event.turnIndex === "number"
        ? event.turnIndex
        : turnOrdinal + 1;
    turnOrdinal = index;
    activeResponseId = newResponseId(`turn-${turnOrdinal}`);
  });

  pi.on("message_update", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    const update = event ? event.assistantMessageEvent : undefined;
    if (!update || typeof update !== "object") return;
    if (update.type === "toolcall_end") {
      await postToolCall(update.toolCall, responseId);
      return;
    }
    if (update.type === "thinking_end") {
      const key = `${turnOrdinal}:${update.contentIndex}`;
      await postReasoningText(update.content, responseId, key);
    }
  });

  pi.on("tool_execution_start", async (_event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
  });

  pi.on("tool_call", async (event, ctx) => {
    rememberContext(ctx);
    const blocked = replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    await postToolCall(
      {
        id: event && event.toolCallId,
        name: event && event.toolName,
        arguments: event && event.input,
      },
      responseId,
    );
    if (blocked) {
      return { block: true, reason: "Interrupted by user" };
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    await postToolResult(event, currentResponseId());
  });

  pi.on("tool_execution_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    await postToolResult(event, currentResponseId());
  });

  pi.on("input", async (event, ctx) => {
    rememberContext(ctx);
    setOmnigentStatus(config, ctx, "running");
    const text = event && typeof event.text === "string" ? event.text : "";
    if (!text) return;
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: `pi-user-${Date.now()}-${++sequence}`,
        item_type: "message",
        item_data: {
          role: "user",
          content: [{ type: "input_text", text }],
        },
      },
    });
  });

  pi.on("message_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    setOmnigentStatus(config, ctx, undefined);
    const message = event ? event.message : undefined;
    const role = messageRole(message);
    if (role !== "assistant") return;
    const responseId = currentResponseId();
    await mirrorAssistantMessage(message, responseId);
    const text = textFromMessage(message);
    if (!text) return;
    await postEvent(config, {
      type: "external_conversation_item",
      data: {
        response_id: responseId,
        item_type: "message",
        item_data: {
          role: "assistant",
          agent: "Pi",
          content: [{ type: "output_text", text }],
        },
      },
    });
  });

  pi.on("turn_end", async (event, ctx) => {
    rememberContext(ctx);
    replayPendingInterrupt(ctx);
    const responseId = currentResponseId();
    await mirrorAssistantMessage(event && event.message, responseId);
    const results =
      event && Array.isArray(event.toolResults) ? event.toolResults : [];
    for (const result of results) {
      await postToolResult(result, responseId);
    }
  });
};
