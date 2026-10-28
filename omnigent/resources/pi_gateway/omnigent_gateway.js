import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { streamSimpleOpenAICompletions } from "@earendil-works/pi-ai";

function signaturesFor(model, context) {
  const calls = new Map();
  for (const message of context.messages) {
    if (
      message.role !== "assistant" ||
      message.provider !== model.provider ||
      message.model !== model.id ||
      message.api !== model.api
    )
      continue;
    for (const block of message.content) {
      if (block.type === "toolCall" && block.thoughtSignature) {
        calls.set(block.id, block.thoughtSignature);
      }
    }
  }
  return calls;
}

function restoreSignatures(payload, signatures) {
  return {
    ...payload,
    messages: payload.messages.map((message) => {
      if (message.role !== "assistant") return message;
      return {
        ...message,
        ...(message.tool_calls
          ? {
              tool_calls: message.tool_calls.map((call) => {
                const thoughtSignature = signatures.get(call.id);
                return thoughtSignature ? { ...call, thoughtSignature } : call;
              }),
            }
          : {}),
      };
    }),
  };
}

function normalizeEvent(frame, state) {
  const lines = frame.split(/\r?\n/);
  const data = lines
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data || data === "[DONE]") return frame;
  let chunk;
  try {
    chunk = JSON.parse(data);
  } catch {
    return frame;
  }
  for (const choice of chunk.choices || []) {
    const delta = choice.delta;
    if (!delta) continue;
    if (Array.isArray(delta.content)) {
      const parts = delta.content.filter((part) => part.type === "text");
      delta.content = parts.map((part) => part.text || "").join("");
    }
    for (const [position, call] of (delta.tool_calls || []).entries()) {
      const key = `${choice.index || 0}:${call.index ?? position}`;
      let entry = state.calls.get(key);
      if (!entry) {
        entry = {};
        state.calls.set(key, entry);
      }
      if (call.id && !entry.id) {
        // Gateway IDs can repeat, and function names may arrive in a later delta.
        entry.id = `call_${randomUUID()}`;
      }
      if (call.id && entry.id) call.id = entry.id;
      const signature = call.thoughtSignature || call.extra_content?.google?.thought_signature;
      if (signature) entry.signature = signature;
    }
  }
  const otherLines = lines.filter((line) => !line.startsWith("data:"));
  return [...otherLines, `data: ${JSON.stringify(chunk)}`].join("\n");
}

function normalizeResponse(response, state) {
  if (
    !response.ok ||
    !response.body ||
    !response.headers.get("content-type")?.includes("text/event-stream")
  )
    return response;
  let pending = "";
  const body = response.body
    .pipeThrough(new TextDecoderStream())
    .pipeThrough(
      new TransformStream({
        transform(chunk, controller) {
          pending += chunk;
          let boundary;
          while ((boundary = /\r?\n\r?\n/.exec(pending))) {
            controller.enqueue(
              normalizeEvent(pending.slice(0, boundary.index), state) + "\n\n",
            );
            pending = pending.slice(boundary.index + boundary[0].length);
          }
        },
        flush(controller) {
          if (pending) controller.enqueue(normalizeEvent(pending, state));
        },
      }),
    )
    .pipeThrough(new TextEncoderStream());
  const headers = new Headers(response.headers);
  headers.delete("content-length");
  headers.delete("content-encoding");
  return new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function attachSignatures(message, state) {
  if (!message?.content) return message;
  for (const block of message.content) {
    if (block.type === "toolCall") {
      const entry = [...state.calls.values()].find((call) => call.id === block.id);
      if (entry?.signature) block.thoughtSignature = entry.signature;
    }
  }
  return message;
}

export function createStream(streamSimple) {
  return (model, context, options = {}) => {
    const state = { calls: new Map() };
    const signatures = signaturesFor(model, context);
    const fetchProvider = options.fetch || globalThis.fetch;
    const stream = streamSimple(model, context, {
      ...options,
      fetch: async (...args) => normalizeResponse(await fetchProvider(...args), state),
      onPayload: async (payload, ...args) => {
        const replacement = await options.onPayload?.(payload, ...args);
        return restoreSignatures(replacement ?? payload, signatures);
      },
    });
    return {
      async *[Symbol.asyncIterator]() {
        for await (const event of stream) {
          attachSignatures(event.partial || event.message || event.error, state);
          yield event;
        }
      },
      async result() {
        return attachSignatures(await stream.result(), state);
      },
    };
  };
}

export default function installGatewayCompatibility(pi) {
  const providers = JSON.parse(
    readFileSync(
      join(dirname(fileURLToPath(import.meta.url)), "omnigent_gateway_providers.json"),
      "utf8",
    ),
  );
  const streamSimple = createStream(streamSimpleOpenAICompletions);
  for (const provider of providers) {
    pi.registerProvider(provider, { api: "openai-completions", streamSimple });
  }
}
