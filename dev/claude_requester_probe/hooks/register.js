// Observe provider-owned identifiers without treating them as human identity.
let sequence = 0;
let incarnation;

async function record($, kind, fields) {
  const directory = await $.env.get('OMNIGENT_CLAUDE_PROBE_DIR');
  if (!directory) return;
  if (!incarnation) incarnation = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const number = ++sequence;
  const event = {
    version: 1, incarnation, sequence: number, session_id: await $.session.id(), kind, ...fields,
  };
  // One file per event avoids append races and keeps partial writes detectable.
  await $.fs.write(`${directory}/${incarnation}-${number}.json`, JSON.stringify(event));
  return number;
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    await record($, 'session.start', {
      session_id: await $.session.id(),
      provider_version: await $.session.version(),
    });
    return next(e);
  });

  on('prompt.submit', async ($, e, next) => {
    const submission = await record($, 'prompt.submit', {
      active_turn_id: e.turnId ?? null,
      origin: e.origin ?? null,
      wait: e.wait ?? null,
      characters: e.text.length,
    });
    try {
      const result = await next(e);
      await record($, 'prompt.result', {
        submission,
        dropped: typeof result?.drop === 'string',
        result_keys: Object.keys(result ?? {}),
      });
      return result;
    } catch (error) {
      await record($, 'prompt.error', { submission });
      throw error;
    }
  });

  on('turn.start', async ($, e, next) => {
    await record($, 'turn.start', { turn_id: e.turnId, agent_id: e.agentId ?? null });
    return next(e);
  });

  on('turn.step', async function* ($, e, next) {
    await record($, 'turn.step', {
      turn_id: e.turnId, step: e.index, agent_id: e.agentId ?? null,
    });
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
          });
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
