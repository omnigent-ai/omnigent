const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const install = require("../pi_native/omnigent_pi_native_extension.js");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const response = (body = {}) => ({ ok: true, status: 200, text: async () => JSON.stringify(body), json: async () => body });

function fixture(t, options = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "omp-bridge-"));
  const inbox = path.join(root, "inbox");
  fs.mkdirSync(inbox);
  const configPath = path.join(root, "config.json");
  fs.writeFileSync(configPath, JSON.stringify({ serverUrl: "http://server", sessionId: "parent", bridgeDir: root, inboxDir: inbox }));
  const prior = { fetch: global.fetch, setInterval: global.setInterval, clearInterval: global.clearInterval, env: process.env.OMNIGENT_OMP_NATIVE_CONFIG };
  process.env.OMNIGENT_OMP_NATIVE_CONFIG = configPath;
  const events = [];
  const requests = [];
  let poll;
  let policy = async () => response({ result: "POLICY_ACTION_ALLOW" });
  global.setInterval = (fn) => { poll = fn; return { poll: true }; };
  global.clearInterval = () => {};
  global.fetch = async (url, request) => {
    const body = request.body ? JSON.parse(request.body) : null;
    requests.push({ url, body, request });
    if (url.endsWith("/policies/evaluate")) return policy(body, request);
    if (url.endsWith("/hooks/omp-child")) return response({ session_id: `child-${body.agent_id}` });
    if (url.endsWith("/events")) events.push(body);
    return response({ item_id: `item-${events.length}` });
  };
  const handlers = {};
  const tools = {};
  const host = { on: (name, fn) => { handlers[name] = fn; }, registerCommand() {}, registerTool: (tool) => { tools[tool.name] = tool; }, getAllTools: () => [], sendUserMessage() {} };
  install(host, { harness: "omp-native", displayName: "OMP", configEnv: "OMNIGENT_OMP_NATIVE_CONFIG", omitIdleDeliveryMode: true, asyncCompaction: true, enforceResultPolicy: true, strictPolicyRequired: true, isolateChildren: true, ...options });
  let nativeId = "native-1";
  const ctx = { agent: { kind: "main", id: "Main" }, isIdle: () => true, abort() {}, modelRegistry: { getAvailable: () => [] }, ui: { setTitle() {}, setStatus() {}, notify() {} }, sessionManager: { getSessionId: () => nativeId, getEntries: () => [], getBranch: () => [], buildSessionContext: () => ({ messages: [] }) } };
  t.after(async () => {
    await handlers.session_shutdown({}, ctx);
    global.fetch = prior.fetch;
    global.setInterval = prior.setInterval;
    global.clearInterval = prior.clearInterval;
    if (prior.env === undefined) delete process.env.OMNIGENT_OMP_NATIVE_CONFIG; else process.env.OMNIGENT_OMP_NATIVE_CONFIG = prior.env;
    fs.rmSync(root, { recursive: true, force: true });
  });
  return { root, inbox, configPath, events, requests, host, ctx, handlers, tools, poll: () => poll(), policy: (fn) => { policy = fn; }, nativeId: (id) => { nativeId = id; } };
}

for (const action of ["ALLOW", "DENY", "ASK", "INVALID"]) {
  test(`native result ${action} never mirrors an unauthorized body`, async (t) => {
    const h = fixture(t, { resultPolicyTimeoutMs: 5 });
    await h.handlers.session_start({}, h.ctx);
    await h.handlers.agent_start({}, h.ctx);
    await h.handlers.tool_call({ toolCallId: "read-1", toolName: "read", input: { path: "secret" } }, h.ctx);
    h.policy(async () => response({ result: `POLICY_ACTION_${action}` }));
    const native = { content: [{ type: "text", text: "SECRET" }, { type: "image", mimeType: "image/png", data: "c2VjcmV0" }], details: { secret: "SECRET" }, isError: false };
    // Exact shallow-reference boundary used by OMP's wrapper.ts/emitToolResult.
    const event = { type: "tool_result", toolName: "read", toolCallId: "read-1", input: {}, ...native };
    const result = await h.handlers.tool_result({ ...event }, h.ctx);
    assert.equal(result.isError, action !== "ALLOW");
    assert.equal(JSON.stringify(result).includes("SECRET"), action === "ALLOW");
    assert(!JSON.stringify(native).includes("SECRET"), "wrapper references are quarantined in place");
    await h.handlers.tool_execution_end({ ...event, result: { content: [{ type: "text", text: "SECRET" }] } }, h.ctx);
    await h.handlers.turn_end({ toolResults: [event] }, h.ctx);
    if (action === "ALLOW") assert.equal(result.content[1].type, "image");
    await tick();
    const outputs = h.events.filter((e) => e.type === "external_conversation_item" && e.data.item_type === "function_call_output");
    assert.equal(outputs.length, 1);
    assert.equal(JSON.stringify(outputs).includes("SECRET"), action === "ALLOW");
    const gate = h.requests.find((r) => r.body?.event?.type === "PHASE_TOOL_RESULT");
    assert.equal(gate.body.event.request_data.name, "read");
    assert(gate.body.event.data.result.includes("SECRET"), "review gate sees the original result");
  });
}

test("expired native result cannot be restored by a late approval", async (t) => {
  const h = fixture(t, { resultPolicyTimeoutMs: 5 });
  await h.handlers.session_start({}, h.ctx);
  let approve;
  h.policy(() => new Promise((resolve) => { approve = resolve; }));
  const native = { content: [{ type: "text", text: "SECRET" }], details: { secret: "SECRET" } };
  const pending = h.handlers.tool_result({ toolName: "read", toolCallId: "late", input: {}, ...native }, h.ctx);
  await tick();
  assert(!JSON.stringify(native).includes("SECRET"));
  const denied = await pending;
  assert.equal(denied.isError, true);
  approve(response({ result: "POLICY_ACTION_ALLOW" }));
  await tick();
  assert(!JSON.stringify(native).includes("SECRET"));
  assert(!JSON.stringify(h.events).includes("SECRET"));
});

test("idle async delivery omits mode, deduplicates in-flight input, and still interrupts", async (t) => {
  const h = fixture(t);
  await h.handlers.session_start({}, h.ctx);
  let finish;
  const sent = [];
  h.host.sendUserMessage = (content, mode) => { sent.push({ content, mode }); return new Promise((resolve) => { finish = resolve; }); };
  fs.writeFileSync(path.join(h.inbox, "001.json"), JSON.stringify({ type: "user_message", id: "one", content: "hello" }));
  const first = h.poll();
  await tick();
  await h.poll();
  assert.deepEqual(sent, [{ content: "hello", mode: undefined }]);
  let aborted = 0;
  h.ctx.isIdle = () => false;
  h.ctx.abort = () => { aborted += 1; };
  fs.writeFileSync(path.join(h.inbox, "002.json"), JSON.stringify({ type: "interrupt" }));
  await h.poll();
  assert.equal(aborted, 1);
  const multimodal = [{ type: "text", text: "look" }, { type: "image", mimeType: "image/png", data: "eA==" }];
  fs.writeFileSync(path.join(h.inbox, "003.json"), JSON.stringify({ type: "user_message", id: "image", content: multimodal }));
  await h.poll();
  assert.equal(sent.length, 1, "a follow-up must not steer into the abort unwind");
  finish();
  await first;
  assert(!fs.existsSync(path.join(h.inbox, "001.json")));
  h.host.sendUserMessage = (content, mode) => { sent.push({ content, mode }); };
  await h.poll();
  assert.equal(sent.length, 1, "delivery must also wait for OMP to report idle");
  h.ctx.isIdle = () => true;
  await h.poll();
  assert.deepEqual(sent[1], { content: multimodal, mode: undefined });
  assert(!fs.existsSync(path.join(h.inbox, "003.json")));
});

test("missing configuration denies native tools", async (t) => {
  const h = fixture(t);
  fs.unlinkSync(h.configPath);
  const handlers = {};
  install({ on: (name, fn) => { handlers[name] = fn; }, registerCommand() {}, getAllTools: () => [] }, { configEnv: "OMNIGENT_OMP_NATIVE_CONFIG", enforceResultPolicy: true, strictPolicyRequired: true });
  const verdict = await handlers.tool_call({ toolName: "bash", toolCallId: "missing", input: {} }, h.ctx);
  assert.equal(verdict.block, true);
  await handlers.session_shutdown();
});

test("native session switch checkpoints replacement context before new telemetry", async (t) => {
  const h = fixture(t);
  await h.handlers.session_start({}, h.ctx);
  h.nativeId("native-2");
  await h.handlers.session_switch({ reason: "new" }, h.ctx);
  const transition = h.requests.find((r) => r.url.endsWith("/hooks/omp-session"));
  assert.equal(transition.body.previous_session_id, "native-1");
  assert.equal(transition.body.native_session_id, "native-2");
  assert.deepEqual(transition.body.compacted_messages, []);
  await h.handlers.input({ text: "fresh", source: "extension" }, h.ctx);
  await tick();
  const prompt = h.events.find((e) => e.data?.item_data?.role === "user");
  assert.equal(prompt.data._omnigent_native_session_id, "native-2");
});

test("rebound children and depth-zero advisors never share parent transcript or inbox", async (t) => {
  const h = fixture(t);
  const { pathToFileURL } = require("node:url");
  fs.copyFileSync(path.join(__dirname, "omnigent_omp_native_extension.mjs"), path.join(h.root, "adapter.mjs"));
  fs.copyFileSync(path.join(__dirname, "../pi_native/omnigent_pi_native_extension.js"), path.join(h.root, "omnigent_pi_native_extension.cjs"));
  const { default: adapter } = await import(pathToFileURL(path.join(h.root, "adapter.mjs")).href);
  adapter(h.host);
  await h.handlers.session_start({}, h.ctx);
  const child = {
    ...h.ctx,
    agent: { kind: "sub", id: "advisor", parentId: "Main", name: "advisor", depth: 0 },
    sessionManager: { ...h.ctx.sessionManager, getSessionId: () => "native-child" },
  };
  await h.handlers.session_start({}, child);
  await h.handlers.agent_start({}, h.ctx);
  await h.handlers.agent_start({}, child);
  await h.handlers.input({ text: "parent-only", source: "extension" }, h.ctx);
  await h.handlers.input({ text: "child-only", source: "extension" }, child);
  await h.handlers.tool_call({ toolName: "read", toolCallId: "child-read", input: {} }, child);
  await tick();
  const mirrors = h.requests.filter((r) => r.url.endsWith("/events"));
  const childPrompt = mirrors.find((r) => r.body.data?.item_data?.content?.[0]?.text === "child-only");
  const parentPrompt = mirrors.find((r) => r.body.data?.item_data?.content?.[0]?.text === "parent-only");
  assert(childPrompt.url.includes("/sessions/child-advisor/"));
  assert(parentPrompt.url.includes("/sessions/parent/"));
  assert(h.requests.some((r) => r.url.includes("/sessions/child-advisor/policies/evaluate")));
  fs.writeFileSync(path.join(h.inbox, "child-isolation.json"), JSON.stringify({ type: "user_message", content: "parent delivery" }));
  let delivered = 0;
  h.host.sendUserMessage = () => { delivered += 1; };
  await h.poll();
  assert.equal(delivered, 1, "only the parent owns the inbox poller");
});

test("interrupt cancels a parked approval and never releases its native tool", async (t) => {
  const h = fixture(t);
  await h.handlers.session_start({}, h.ctx);
  await h.handlers.agent_start({}, h.ctx);
  h.ctx.isIdle = () => false;
  h.policy((_body, request) => new Promise((_resolve, reject) => {
    request.signal.addEventListener("abort", () => reject(new Error("cancelled")), { once: true });
  }));
  const call = h.handlers.tool_call({ toolName: "bash", toolCallId: "cancelled", input: {} }, h.ctx);
  await tick();
  fs.writeFileSync(path.join(h.inbox, "interrupt.json"), JSON.stringify({ type: "interrupt" }));
  await h.poll();
  assert.equal((await call).block, true);
});

test("OMP promise compaction and native user images survive checkpointing", async (t) => {
  const h = fixture(t);
  let completed = false;
  h.ctx.compact = async (instructions) => { assert.equal(instructions, "keep images"); completed = true; };
  const image = { type: "image", mimeType: "image/png", data: "eA==" };
  h.ctx.sessionManager.buildSessionContext = () => ({ messages: [{ role: "user", content: [{ type: "text", text: "look" }, image] }] });
  await h.handlers.session_start({}, h.ctx);
  fs.writeFileSync(path.join(h.inbox, "compact.json"), JSON.stringify({ type: "compact", custom_instructions: "keep images" }));
  await h.poll();
  await tick();
  assert.equal(completed, true);
  await h.handlers.input({ text: "look", images: [image], source: "extension" }, h.ctx);
  await h.handlers.session_switch({ reason: "resume" }, h.ctx);
  const checkpoint = h.requests.find((r) => r.url.endsWith("/hooks/omp-session"));
  assert.equal(checkpoint.body.compacted_messages[0].content[1].image_url, "data:image/png;base64,eA==");
  const prompt = h.events.find((e) => e.data?.item_data?.role === "user");
  assert.equal(prompt.data.item_data.content[1].image_url, "data:image/png;base64,eA==");
});

for (const resultHook of [false, true]) {
  test(`known native call denial mirrors only its safe reason (tool_result=${resultHook})`, async (t) => {
    const h = fixture(t);
    await h.handlers.session_start({}, h.ctx);
    await h.handlers.agent_start({}, h.ctx);
    h.policy(async () => response({ result: "POLICY_ACTION_DENY", reason: "Root policy denies this call" }));
    const call = { toolCallId: "denied-call", toolName: "bash", input: {} };
    assert.equal((await h.handlers.tool_call(call, h.ctx)).block, true);
    const native = { ...call, content: [{ type: "text", text: "UNREVIEWED SECRET" }], result: { content: [{ type: "text", text: "UNREVIEWED SECRET" }] } };
    if (resultHook) {
      const safe = await h.handlers.tool_result(native, h.ctx);
      assert.equal(safe.isError, true);
      assert.equal(safe.content[0].text, "Root policy denies this call");
    }
    await h.handlers.tool_execution_end(native, h.ctx);
    await h.handlers.turn_end({ toolResults: [native] }, h.ctx);
    await tick();
    const outputs = h.events.filter((event) => event.type === "external_conversation_item" && event.data.item_type === "function_call_output");
    assert.equal(outputs.length, 1);
    assert.equal(outputs[0].data.item_data.output, "Root policy denies this call");
    assert(!JSON.stringify(outputs).includes("SECRET"));
    assert(!h.requests.some((request) => request.body?.event?.type === "PHASE_TOOL_RESULT"), "an unexecuted call has no result to review");
  });
}

test("OMP native formDialog uses the authenticated web dialog transport", async (t) => {
  let originalCalled = false;
  const schema = { type: "object", properties: { f0: { type: "number", default: 2 } } };
  const h = fixture(t, { formDialog: async (_html, opts, park) => park({ method: "formDialog", title: "Workers", requestedSchema: schema }, opts) });
  h.ctx.mode = "tui";
  h.ctx.ui.formDialog = () => { originalCalled = true; };
  await h.handlers.session_start({}, h.ctx);
  await h.ctx.ui.formDialog("<form><input type='number'></form>", { timeout: 1000 });
  const request = h.requests.find((request) => request.url.endsWith("/hooks/pi-extension-ui"));
  assert.equal(request.body.request.method, "formDialog");
  assert.deepEqual(request.body.request.requestedSchema, schema);
  assert.match(request.body.elicitation_id, /^elicit_omp_[0-9a-f]{32}$/);
  assert(!Object.hasOwn(request.body.request, "html"));
  assert.equal(originalCalled, false);
});
