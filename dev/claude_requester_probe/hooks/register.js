// Observe provider-owned identifiers without treating them as human identity.
let sequence = 0;
let incarnation;

async function capture($) {
  const directory = await $.env.get('OMNIGENT_CLAUDE_PROBE_DIR');
  return directory ? { directory, sessionId: await $.session.id() } : null;
}

async function record($, kind, fields, scope) {
  if (scope === undefined) scope = await capture($);
  if (!scope) return;
  if (!incarnation) incarnation = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const number = ++sequence;
  const event = {
    version: 1, incarnation, sequence: number, session_id: scope.sessionId, kind, ...fields,
  };
  // One file per event avoids append races and keeps partial writes detectable.
  await $.fs.write(`${scope.directory}/${incarnation}-${number}.json`, JSON.stringify(event));
  return number;
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    await record($, 'session.start', {
      provider_version: await $.session.version(),
    });
    return next(e);
  });

  on('prompt.submit', async ($, e, next) => {
    const scope = await capture($);
    const submission = await record($, 'prompt.submit', {
      active_turn_id: e.turnId ?? null,
      origin: e.origin ?? null,
      wait: e.wait ?? null,
      characters: e.text.length,
    }, scope);
    try {
      const result = await next(e);
      await record($, 'prompt.result', {
        submission,
        dropped: typeof result?.drop === 'string',
        result_keys: Object.keys(result ?? {}),
      }, scope);
      return result;
    } catch (error) {
      await record($, 'prompt.error', { submission }, scope);
      throw error;
    }
  });

  on('turn.start', async ($, e, next) => {
    await record($, 'turn.start', { turn_id: e.turnId, agent_id: e.agentId ?? null });
    return next(e);
  });

  on('session.append', async ($, e, next) => {
    const scope = await capture($);
    const appended = await record($, 'session.append', {
      row_uuid: e.uuid, door: e.door, agent_id: e.agentId ?? null,
      origin_kind: e.origin?.kind ?? null,
    }, scope);
    const result = await next(e);
    await record($, 'append.result', {
      appended, row_uuid: result?.uuid ?? null, refused: typeof result?.deny === 'string',
    }, scope);
    return result;
  });

  on('turn.step', async function* ($, e, next) {
    const scope = await capture($);
    await record($, 'turn.step', {
      turn_id: e.turnId, step: e.index, agent_id: e.agentId ?? null,
    }, scope);
    const stream = next(e);
    let completed = false;
    try {
      for (;;) {
        const chunk = await stream.next();
        if (chunk.done) {
          completed = true;
          return chunk.value;
        }
        if (chunk.value.kind === 'tool') {
          await record($, 'tool.emitted', {
            turn_id: e.turnId, step: e.index, agent_id: e.agentId ?? null,
            tool_use_id: chunk.value.id, tool: chunk.value.name,
          }, scope);
        }
        yield chunk.value;
      }
    } finally {
      if (!completed) await stream.return();
    }
  });

  on('tool.call', async ($, e, next) => {
    await record($, 'tool.call', {
      tool_use_id: e.tool_use_id, tool: e.tool, agent_id: e.agentId ?? null,
    });
    return next(e);
  });

  on('turn.complete', async ($, e, next) => {
    await record($, 'turn.complete', {
      turn_id: e.turnId, agent_id: e.agentId ?? null, aborted: e.isAborted ?? null,
    });
    return next(e);
  });

  on('session.end', async ($, e, next) => {
    await record($, 'session.end', { reason: e.reason });
    return next(e);
  });
}
