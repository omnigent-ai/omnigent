import assert from 'node:assert/strict';
import { test } from 'node:test';
import { register } from '../../dev/claude_requester_probe/hooks/register.js';

function harness() {
  const hooks = new Map();
  const records = [];
  register((event, callback) => hooks.set(event, callback));
  const api = {
    env: { get: async () => '/probe' },
    fs: { write: async (path, data) => {
      assert.ok(path.startsWith('/probe/'));
      records.push(JSON.parse(data));
    } },
    session: { id: async () => 'session', version: async () => ({ version: 'fixture' }) },
  };
  return { hooks, records, api };
}

test('streaming preserves chunks/result and captures the original turn before dispatch', async () => {
  const { hooks, records, api } = harness();
  const first = { kind: 'tool', id: 'old-call', name: 'Read' };
  const opaque = { kind: 'engine', ref: 4 };
  const result = { turnId: 'old', answer: '' };
  async function* provider() { yield first; yield opaque; return result; }
  const stream = hooks.get('turn.step')(api, { turnId: 'old', index: 0 }, provider);
  assert.deepEqual(await stream.next(), { value: first, done: false });
  assert.equal(records.at(-1).tool_use_id, 'old-call');
  await hooks.get('turn.start')(api, { turnId: 'new' }, async e => e);
  assert.deepEqual(await stream.next(), { value: opaque, done: false });
  assert.deepEqual(await stream.next(), { value: result, done: true });
  await hooks.get('tool.call')(api, { tool_use_id: 'old-call', tool: 'Read' }, async () => 'original');
  assert.equal(records.find(e => e.kind === 'tool.emitted').turn_id, 'old');
  assert.equal(records.at(-1).kind, 'tool.call');
});

test('observes queued, dropped and failed prompts without saving text or assigning a requester', async () => {
  const { hooks, records, api } = harness();
  const prompt = { text: 'private prompt', turnId: 'running', wait: true, origin: { kind: 'composer' } };
  const refusal = { drop: 'no' };
  assert.equal(await hooks.get('prompt.submit')(api, prompt, async () => refusal), refusal);
  assert.equal(records[0].active_turn_id, 'running');
  assert.equal(records[0].wait, true);
  assert.equal(records[1].dropped, true);
  const failure = new Error('provider error');
  await assert.rejects(hooks.get('prompt.submit')(api, prompt, async () => { throw failure; }), error => error === failure);
  assert.equal(records.at(-1).kind, 'prompt.error');
  assert.ok(!JSON.stringify(records).includes('private prompt'));
  assert.ok(records.every(record => !('requester' in record)));
});

test('missing opt-in directory has no writes or change to the tool result', async () => {
  const { hooks, records, api } = harness();
  api.env.get = async () => undefined;
  const result = { result: 'unchanged' };
  assert.equal(await hooks.get('tool.call')(api, { tool: 'Read' }, async () => result), result);
  assert.deepEqual(records, []);
});

test('closing an interrupted observation closes the provider stream', async () => {
  const { hooks, api } = harness();
  let closed = false;
  async function* provider() {
    try { yield { kind: 'engine', ref: 1 }; } finally { closed = true; }
  }
  const stream = hooks.get('turn.step')(api, { turnId: 'one', index: 0 }, provider);
  await stream.next();
  await stream.return();
  assert.equal(closed, true);
});

test('retained helper streams and prompt results keep their entry session after reset', async () => {
  const { hooks, records, api } = harness();
  let session = 'before-clear';
  api.session.id = async () => session;
  async function* provider() {
    yield { kind: 'engine', ref: 1 };
    yield { kind: 'tool', id: 'helper-call', name: 'Read' };
  }
  const stream = hooks.get('turn.step')(api, { turnId: 'helper-turn', agentId: 'helper', index: 0 }, provider);
  await stream.next();
  const prompt = { text: 'private', wait: false, origin: { kind: 'composer' } };
  const rewritten = { text: 'also private', origin: { kind: 'composer' } };
  assert.equal(await hooks.get('prompt.submit')(api, prompt, async () => {
    session = 'after-clear';
    return rewritten;
  }), rewritten);
  await stream.next();
  await stream.next();
  assert.equal(records.find(e => e.kind === 'prompt.result').session_id, 'before-clear');
  const emitted = records.find(e => e.kind === 'tool.emitted');
  assert.equal(emitted.session_id, 'before-clear');
  assert.equal(emitted.agent_id, 'helper');
  await hooks.get('tool.call')(api, { tool_use_id: 'helper-call', tool: 'Read', agentId: 'helper' }, async () => ({}));
  assert.equal(records.at(-1).session_id, 'after-clear');
  assert.ok(!JSON.stringify(records).includes('private'));
});

test('append observes row identity without saving message contents or claiming consumption', async () => {
  const { hooks, records, api } = harness();
  const row = { uuid: 'row-id', door: 'prompt', origin: { kind: 'composer' }, message: { content: 'private' } };
  const result = { uuid: 'row-id', message: { content: 'rewritten private' } };
  assert.equal(await hooks.get('session.append')(api, row, async () => result), result);
  assert.equal(records[0].row_uuid, 'row-id');
  assert.equal(records[1].row_uuid, 'row-id');
  assert.equal(records[1].appended, records[0].sequence);
  assert.ok(!JSON.stringify(records).includes('private'));
  const refusal = { deny: 'private refusal' };
  assert.equal(await hooks.get('session.append')(api, row, async () => refusal), refusal);
  assert.equal(records.at(-1).refused, true);
  assert.equal(records.at(-1).row_uuid, null);
  assert.ok(!JSON.stringify(records).includes('private'));
});
