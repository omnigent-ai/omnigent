import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const { default: install, createStream } = await import(pathToFileURL(process.argv[2]));
const registered = [];
install({ registerProvider: (name, config) => registered.push({ name, config }) });
assert.deepEqual(
  registered.map(({ name }) => name),
  ["gateway"],
);
assert.deepEqual(Object.keys(registered[0].config).sort(), ["api", "streamSimple"]);

const model = { id: "model", provider: "gateway", api: "openai-completions" };
const historicalCall = {
  type: "toolCall",
  id: "prior",
  name: "echo",
  arguments: {},
  thoughtSignature: "prior-signature",
};
const context = {
  messages: [
    { role: "assistant", ...model, model: model.id, content: [historicalCall] },
    {
      role: "assistant",
      ...model,
      provider: "different-provider",
      model: model.id,
      content: [{ ...historicalCall, id: "other", thoughtSignature: "must-not-leak" }],
    },
  ],
};
const payload = {
  messages: [{ role: "assistant", tool_calls: [{ id: "prior" }, { id: "other" }] }],
};
let sent;
let responseObserved = false;
const frames = [
  {
    choices: [
      {
        index: 0,
        delta: {
          content: [
            { type: "reasoning", text: "private" },
            { type: "text", text: "héllo" },
          ],
          tool_calls: [
            { index: 0, id: "echo", function: { name: "echo", arguments: "{}" } },
            { index: 1, id: "echo", function: { name: "echo", arguments: "{}" } },
          ],
        },
      },
    ],
  },
  {
    choices: [
      {
        index: 0,
        delta: {
          tool_calls: [
            { index: 0, thoughtSignature: "sig-0" },
            { index: 1, extra_content: { google: { thought_signature: "sig-1" } } },
          ],
        },
      },
    ],
  },
  { choices: [], usage: { total_tokens: 7 } },
];
const splitMetadata = process.argv[3] === "split";
if (splitMetadata) {
  const calls = frames[0].choices[0].delta.tool_calls;
  const delayedNames = calls.map(({ index, id, function: fn }) => ({
    index,
    id,
    function: fn,
  }));
  for (const call of calls) delete call.function;
  frames.splice(1, 0, {
    choices: [{ index: 0, delta: { tool_calls: delayedNames } }],
  });
}
const wire =
  ": comment\r\n\r\n" +
  frames.map((frame) => "data: " + JSON.stringify(frame) + "\r\n\r\n").join("") +
  "data: [DONE]\r\n\r\n";
const fetchProvider = async (_url, init) => {
  sent = JSON.parse(init.body);
  const bytes = new TextEncoder().encode(wire);
  return new Response(
    new ReadableStream({
      start(controller) {
        // Split every UTF-8 character, JSON token and CRLF boundary across chunks.
        for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
        controller.close();
      },
    }),
    {
      headers: {
        "content-type": "text/event-stream",
        "content-length": String(bytes.length),
        "content-encoding": "gzip",
        "x-request-id": "preserved",
      },
    },
  );
};
let observed;
function provider(_model, _context, options) {
  const done = (async () => {
    const outgoing = await options.onPayload(payload, model);
    const response = await options.fetch("https://example.test", {
      body: JSON.stringify(outgoing),
    });
    await options.onResponse({
      status: response.status,
      headers: Object.fromEntries(response.headers),
    });
    assert.equal(response.headers.get("content-length"), null);
    assert.equal(response.headers.get("content-encoding"), null);
    assert.equal(response.headers.get("x-request-id"), "preserved");
    const text = await response.text();
    observed = text
      .split("\n")
      .filter((line) => line.startsWith("data: ") && !line.includes("[DONE]"))
      .map((line) => JSON.parse(line.slice(6)));
    const delta = observed[0].choices[0].delta;
    return {
      role: "assistant",
      content: [
        { type: "text", text: delta.content },
        ...delta.tool_calls.map((call) => ({
          type: "toolCall",
          id: call.id,
          name: "echo",
          arguments: {},
        })),
      ],
    };
  })();
  return {
    result: () => done,
    async *[Symbol.asyncIterator]() {
      const message = await done;
      yield { type: "done", message };
    },
  };
}
const output = createStream(provider)(model, context, {
  fetch: fetchProvider,
  onPayload: (original) => ({ ...original, caller_override: true }),
  onResponse: () => {
    responseObserved = true;
  },
});
const events = [];
for await (const event of output) events.push(event);
const result = await output.result();
assert.equal(result, events[0].message);
assert.equal(responseObserved, true);
assert.equal(result.content[0].text, "héllo");
assert.equal(sent.caller_override, true);
assert.equal(sent.messages[0].tool_calls[0].thoughtSignature, "prior-signature");
assert.equal(sent.messages[0].tool_calls[1].thoughtSignature, undefined);
assert.equal(payload.messages[0].tool_calls[0].thoughtSignature, undefined);
assert.deepEqual(
  result.content.slice(1).map((call) => call.thoughtSignature),
  ["sig-0", "sig-1"],
);
const ids = result.content.slice(1).map((call) => call.id);
assert.equal(new Set(ids).size, 2);
assert.ok(ids.every((id) => id !== "echo"));
const namedCalls = observed[splitMetadata ? 1 : 0].choices[0].delta.tool_calls;
assert.deepEqual(
  namedCalls.map((call) => call.function.name),
  ["echo", "echo"],
);
assert.deepEqual(namedCalls.map((call) => call.id), ids);
assert.equal(observed.at(-2).choices[0].delta.tool_calls[0].id, undefined);
assert.deepEqual(observed.at(-1), frames.at(-1));

const errorResponse = new Response("provider error", { status: 429 });
let options;
createStream((_model, _context, received) => {
  options = received;
  return {};
})(model, { messages: [] }, { fetch: async () => errorResponse });
assert.equal(await options.fetch("unused"), errorResponse);
let cancelled = false;
const response = new Response(
  new ReadableStream({
    cancel() {
      cancelled = true;
    },
  }),
  { headers: { "content-type": "text/event-stream" } },
);
createStream((_model, _context, received) => {
  options = received;
  return {};
})(
  model,
  { messages: [] },
  {
    fetch: async () => response,
  },
);
const normalized = await options.fetch("unused");
await normalized.body.cancel();
await new Promise((resolve) => setImmediate(resolve));
assert.equal(cancelled, true);
console.log(
  "fragmentation, signatures, call identities, callbacks, errors and cancellation passed",
);
