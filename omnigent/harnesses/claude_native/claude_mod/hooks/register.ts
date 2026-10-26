import type { EngineInterface, Register } from 'claude-code'

// Mod side of the Omnigent Claude channel. The serve-mcp control server owns
// the protocol (omnigent/harnesses/claude_native/mod_channel.py): this module
// heartbeats, reports turn boundaries, and runs the commands Omnigent sends.

const PROTOCOL_VERSION = 1
const HEARTBEAT_MS = 30_000
const RETRY_BASE_MS = 500
const RETRY_MAX_MS = 5_000
// A turn event is sent this many times, with a short backoff, before it is dropped.
const EVENT_SEND_ATTEMPTS = 5
const EVENT_RETRY_BASE_MS = 250
// A model, effort or compact command is acknowledged as started after this
// long, so a switch the dialog holds open does not hold the harness.
const ACK_AFTER_MS = 1_500
// An abort is answered once the turn it cancelled has ended, so the caller knows
// the turn stopped.
const ABORT_END_WAIT_MS = 5_000
// After a submit enters, the next command waits this long at most for its turn to
// start, so a command that needs an idle session sees that turn as busy.
const TURN_START_WAIT_MS = 5_000
const ERROR_MAX_CHARS = 2_000
const MOD_VERSION_FALLBACK = 'unknown'

type Json = Record<string, unknown>

interface Channel {
  readonly token: string
  readonly socketPath?: string
  readonly url?: string
}

interface Outgoing {
  readonly path: string
  readonly body: Json
  attempts: number
}

interface ModState {
  readonly bridgeDir: string
  modVersion: string
  mainTurnId: string | undefined
  // Main-loop turns started so far, and the submits waiting for the next one.
  mainTurnStarts: number
  readonly turnStartWaiters: Set<() => void>
  readonly outbox: Outgoing[]
  helloPending: boolean
  wake: (() => void) | undefined
  // Every command but abort runs after the one before it has finished.
  commandChain: Promise<void>
}

interface Command {
  readonly id: string
  readonly op: string
  readonly args: Json
}

let state: ModState | undefined
// Resolvers for an abort still waiting on its turn's end, keyed by turn id.
const turnEndWaiters = new Map<string, () => void>()

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    void startChannel($)
    return started
  })

  on('turn.start', async ($, e, next) => {
    if (state !== undefined) {
      if (e.agentId === undefined) noteMainTurnStart(state, e.turnId)
      queueEvent(state, {
        kind: 'turn.start',
        turn_id: e.turnId,
        agent_id: e.agentId ?? null,
        claude_session_id: await $.session.id(),
      })
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const endWaiter = turnEndWaiters.get(e.turnId)
    if (endWaiter !== undefined) {
      turnEndWaiters.delete(e.turnId)
      endWaiter()
    }
    if (state !== undefined) {
      if (e.agentId === undefined) state.mainTurnId = undefined
      queueEvent(state, {
        kind: 'turn.complete',
        turn_id: e.turnId,
        agent_id: e.agentId ?? null,
        reason: e.reason,
        claude_session_id: await $.session.id(),
      })
    }
    return next(e)
  })
}

async function startChannel($: EngineInterface): Promise<void> {
  if (state !== undefined) return
  // Set before the first await, so a turn that starts meanwhile is still tracked.
  const started: ModState = {
    bridgeDir: bridgeDirectoryOf($.plugin.root),
    modVersion: MOD_VERSION_FALLBACK,
    mainTurnId: undefined,
    mainTurnStarts: 0,
    turnStartWaiters: new Set(),
    outbox: [],
    helloPending: true,
    wake: undefined,
    commandChain: Promise.resolve(),
  }
  state = started
  started.modVersion = await readModVersion($, $.plugin.root)
  $.clock.every(HEARTBEAT_MS, () => requestHello(started))
  void drainOutbox($, started)
  void pollCommands($, started)
}

function noteMainTurnStart(state: ModState, turnId: string): void {
  state.mainTurnId = turnId
  state.mainTurnStarts += 1
  for (const wake of state.turnStartWaiters) wake()
  state.turnStartWaiters.clear()
}

// Resolves once a main-loop turn has started after *startsBefore*, or after the wait.
async function waitForMainTurnStart(
  $: EngineInterface,
  state: ModState,
  startsBefore: number,
): Promise<void> {
  if (state.mainTurnStarts > startsBefore) return
  let wake: () => void = () => undefined
  const turnStarted = new Promise<void>(resolve => {
    wake = resolve
  })
  state.turnStartWaiters.add(wake)
  await Promise.race([turnStarted, $.clock.sleep(TURN_START_WAIT_MS)])
  state.turnStartWaiters.delete(wake)
}

// The mod is loaded from <bridge dir>/claude-mod, so the bridge dir is its parent.
function bridgeDirectoryOf(pluginRoot: string): string {
  const trimmed = pluginRoot.replace(/[\\/]+$/, '')
  const cut = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
  return cut > 0 ? trimmed.slice(0, cut) : trimmed
}

async function readModVersion($: EngineInterface, pluginRoot: string): Promise<string> {
  try {
    const manifest = JSON.parse(await $.fs.read(`${pluginRoot}/.claude-plugin/plugin.json`)) as Json
    return typeof manifest.version === 'string' ? manifest.version : MOD_VERSION_FALLBACK
  } catch {
    return MOD_VERSION_FALLBACK
  }
}

function backoffMs(failures: number): number {
  return Math.min(RETRY_BASE_MS * 2 ** Math.min(failures, 4), RETRY_MAX_MS)
}

// One read of server.json, or undefined while it is missing or being rewritten.
async function tryReadChannel($: EngineInterface, bridgeDir: string): Promise<Channel | undefined> {
  try {
    const server = JSON.parse(await $.fs.read(`${bridgeDir}/server.json`)) as Json
    if (typeof server.token === 'string') {
      if (typeof server.socket === 'string') return { token: server.token, socketPath: server.socket }
      if (typeof server.url === 'string') return { token: server.token, url: server.url }
    }
  } catch {
    // Not written yet, or being rewritten.
  }
  return undefined
}

// The serve-mcp process writes server.json when it starts, so wait for it; a
// restarted server writes a new socket and token, so every caller re-reads it.
async function readChannel($: EngineInterface, bridgeDir: string): Promise<Channel> {
  for (let attempt = 0; ; attempt += 1) {
    const channel = await tryReadChannel($, bridgeDir)
    if (channel !== undefined) return channel
    await $.clock.sleep(backoffMs(attempt))
  }
}

function request(
  $: EngineInterface,
  channel: Channel,
  method: 'GET' | 'POST',
  path: string,
  body?: Json,
) {
  const headers = {
    Authorization: `Bearer ${channel.token}`,
    'Content-Type': 'application/json',
  }
  const serialized = body === undefined ? undefined : JSON.stringify(body)
  if (channel.socketPath !== undefined) {
    return $.http.fetch(`http://localhost${path}`, {
      method,
      headers,
      body: serialized,
      socketPath: channel.socketPath,
    })
  }
  return $.http.fetch(`${channel.url}${path}`, { method, headers, body: serialized })
}

function requestHello(state: ModState): void {
  state.helloPending = true
  state.wake?.()
  state.wake = undefined
}

function queueEvent(state: ModState, body: Json): void {
  state.outbox.push({ path: '/mod/event', body: { v: PROTOCOL_VERSION, ...body }, attempts: 0 })
  state.wake?.()
  state.wake = undefined
}

async function helloBody($: EngineInterface, state: ModState): Promise<Json> {
  const version = await $.session.version()
  return {
    v: PROTOCOL_VERSION,
    mod_version: state.modVersion,
    claude_version: version.version,
    claude_session_id: await $.session.id(),
    main_turn_id: state.mainTurnId ?? null,
  }
}

// One writer for the heartbeat and the turn events, so they reach the server
// in the order they were made. A transient failure is retried after a short
// backoff, re-reading server.json each time; an event that still fails is dropped.
async function drainOutbox($: EngineInterface, state: ModState): Promise<void> {
  let channel = await readChannel($, state.bridgeDir)
  let helloFailures = 0
  for (;;) {
    const sendingHello = state.helloPending
    if (!sendingHello && state.outbox.length === 0) {
      await new Promise<void>(resolve => {
        state.wake = resolve
      })
      continue
    }
    const next: Outgoing = sendingHello
      ? { path: '/mod/hello', body: await helloBody($, state), attempts: 0 }
      : state.outbox[0]
    state.helloPending = false
    const status = await postOnce($, channel, next)
    if (status === 200 || (status >= 400 && status < 500 && status !== 401)) {
      // Stored, or a body the server will never accept: either way it leaves the queue.
      if (!sendingHello) state.outbox.shift()
      helloFailures = 0
      continue
    }
    if (sendingHello) {
      state.helloPending = true
      channel = await readChannel($, state.bridgeDir)
      await $.clock.sleep(backoffMs(helloFailures))
      helloFailures += 1
      continue
    }
    next.attempts += 1
    if (next.attempts >= EVENT_SEND_ATTEMPTS) {
      state.outbox.shift()
      continue
    }
    channel = (await tryReadChannel($, state.bridgeDir)) ?? channel
    await $.clock.sleep(EVENT_RETRY_BASE_MS * 2 ** (next.attempts - 1))
  }
}

// The HTTP status of one POST, or 0 when the request never got an answer.
async function postOnce($: EngineInterface, channel: Channel, outgoing: Outgoing): Promise<number> {
  try {
    return (await request($, channel, 'POST', outgoing.path, outgoing.body)).status
  } catch {
    return 0
  }
}

async function pollCommands($: EngineInterface, state: ModState): Promise<void> {
  let channel = await readChannel($, state.bridgeDir)
  let failures = 0
  for (;;) {
    let status: number
    let text = ''
    try {
      const response = await request($, channel, 'GET', '/mod/next')
      status = response.status
      text = response.text
    } catch {
      status = 0
    }
    if (status === 200) {
      failures = 0
      try {
        dispatchCommand($, state, channel, JSON.parse(text) as Command)
      } catch {
        // A malformed command has no id to answer; the harness times out on it.
      }
    } else if (status === 204) {
      failures = 0
    } else {
      channel = await readChannel($, state.bridgeDir)
      await $.clock.sleep(backoffMs(failures))
      failures += 1
    }
  }
}

// An abort runs at once, beside the turn it cancels. Every other command needs an
// idle session, so they run one at a time and two of them never both pass requireIdle.
function dispatchCommand(
  $: EngineInterface,
  state: ModState,
  channel: Channel,
  command: Command,
): void {
  if (command.op === 'abort') {
    void runCommand($, state, channel, command)
    return
  }
  state.commandChain = state.commandChain
    .then(() => runCommand($, state, channel, command))
    .catch(() => undefined)
}

async function runCommand(
  $: EngineInterface,
  state: ModState,
  channel: Channel,
  command: Command,
): Promise<void> {
  const startsBefore = state.mainTurnStarts
  let reply: Json
  let entered = false
  try {
    const result = await performOperation($, state, command.op, command.args)
    reply = { v: PROTOCOL_VERSION, id: command.id, ok: true, result }
    entered = command.op === 'submit' && result.entered === true
  } catch (error) {
    reply = { v: PROTOCOL_VERSION, id: command.id, ok: false, error: describeError(error) }
  }
  try {
    await request($, channel, 'POST', '/mod/result', reply)
  } catch {
    // The harness gave up on the command; there is nobody left to answer.
  }
  // Answered first, so the harness is not kept waiting on the turn's start.
  if (entered) await waitForMainTurnStart($, state, startsBefore)
}

// Only the closed set of operations the server allowlists reaches this switch.
async function performOperation(
  $: EngineInterface,
  state: ModState,
  operation: string,
  args: Json,
): Promise<Json> {
  switch (operation) {
    case 'submit': {
      requireIdle(state)
      // asUser: the text is the person's own words, read bare by the model.
      const submitted = await $.prompt.submit({ text: String(args.text), asUser: true })
      return { entered: submitted.drop === undefined }
    }
    case 'abort': {
      const turnId = state.mainTurnId
      if (turnId === undefined) throw new Error('no main-loop turn is running')
      if (turnEndWaiters.has(turnId)) throw new Error('this turn is already being aborted')
      const turnEnded = new Promise<void>(resolve => {
        turnEndWaiters.set(turnId, resolve)
      })
      try {
        await $.turn.abort({ turnId })
        const ended = await Promise.race([
          turnEnded.then(() => true),
          $.clock.sleep(ABORT_END_WAIT_MS).then(() => false),
        ])
        return { ended }
      } finally {
        turnEndWaiters.delete(turnId)
      }
    }
    case 'model': {
      requireIdle(state)
      // A /model command raises the "Switch model?" dialog on a warm cache, which
      // only a person can answer; the config row switches without it.
      const written = await $.config.set({ key: 'model', value: String(args.model) })
      if (written.deny !== undefined) throw new Error(`model refused: ${written.deny}`)
      return {}
    }
    case 'effort':
      requireIdle(state)
      return acknowledgeWhenStarted($, $.command.run({ command: 'effort', args: String(args.level) }))
    case 'compact':
      requireIdle(state)
      return acknowledgeWhenStarted(
        $,
        $.session.compact(
          typeof args.instructions === 'string' ? { instructions: args.instructions } : undefined,
        ),
      )
    default:
      throw new Error(`unknown operation: ${operation}`)
  }
}

function requireIdle(state: ModState): void {
  if (state.mainTurnId !== undefined) throw new Error('busy: a turn is running')
}

// Resolves as started when the work is still running after ACK_AFTER_MS, and
// rethrows when it fails inside that window.
async function acknowledgeWhenStarted($: EngineInterface, work: Promise<unknown>): Promise<Json> {
  const outcome: { settled: boolean; failure?: { error: unknown } } = { settled: false }
  const tracked = work.then(
    () => {
      outcome.settled = true
    },
    (error: unknown) => {
      outcome.failure = { error }
      outcome.settled = true
    },
  )
  await Promise.race([tracked, $.clock.sleep(ACK_AFTER_MS)])
  if (outcome.failure !== undefined) throw outcome.failure.error
  return { started: true, settled: outcome.settled }
}

function describeError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  return message.slice(0, ERROR_MAX_CHARS)
}
