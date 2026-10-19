import { create } from "zustand";
import {
  CODE_RUN_OUTPUT_MAX_CHARS,
  readCodeRunSegments,
  type CodeRunFinishedStatus,
  type CodeRunLanguage,
  type CodeRunSegment,
  type CodeRunStream,
} from "@/lib/codeRuns";

/** One run of a chat code block, merged from its saved items and live events. */
export interface CodeRun {
  runId: string;
  messageId: string | null;
  blockHash: string | null;
  language: CodeRunLanguage | null;
  command: string;
  /** Epoch seconds. */
  startedAt: number | null;
  output: CodeRunSegment[];
  /** Characters of output received so far; live batches before it are dropped. */
  received: number;
  finished: {
    status: CodeRunFinishedStatus;
    exitCode: number | null;
    durationMs: number;
    truncated: boolean;
  } | null;
  /** Seen running on the current stream connection (live event or snapshot). */
  live: boolean;
  /** Live output passed the saved-output cap, so later batches are not kept. */
  liveCut: boolean;
}

export interface SessionCodeRuns {
  runs: Record<string, CodeRun>;
  /** The current connection's snapshot of active runs has arrived. */
  listingSettled: boolean;
  /** Learned from a refused start: the sandbox uses copy-on-write roots. */
  copyOnWrite: boolean;
  /** Refusal reasons by block key, shown only on this device. */
  refusals: Record<string, string>;
}

export type CodeRunState = CodeRunFinishedStatus | "running" | "interrupted" | "waiting";

type WireItem = Record<string, unknown>;

interface CodeRunStoreState {
  sessions: Record<string, SessionCodeRuns>;
  ingestItems: (sessionId: string, items: readonly unknown[]) => void;
  started: (sessionId: string, item: WireItem) => void;
  finished: (sessionId: string, item: WireItem) => void;
  output: (
    sessionId: string,
    runId: string,
    offset: number,
    stream: CodeRunStream,
    text: string,
  ) => void;
  /** {@link output}, coalesced to at most one store update per animation frame. */
  queueOutput: (
    sessionId: string,
    runId: string,
    offset: number,
    stream: CodeRunStream,
    text: string,
  ) => void;
  snapshot: (sessionId: string, runId: string, offset: number, output: CodeRunSegment[]) => void;
  accepted: (
    sessionId: string,
    runId: string,
    run: { messageId: string; blockHash: string; language: CodeRunLanguage; command: string },
  ) => void;
  streamOpened: (sessionId: string) => void;
  listingSettled: (sessionId: string) => void;
  setRefusal: (sessionId: string, blockKey: string, reason: string | null) => void;
  setCopyOnWrite: (sessionId: string) => void;
}

const EMPTY_SESSION: SessionCodeRuns = {
  runs: {},
  listingSettled: false,
  copyOnWrite: false,
  refusals: {},
};

function emptyRun(runId: string): CodeRun {
  return {
    runId,
    messageId: null,
    blockHash: null,
    language: null,
    command: "",
    startedAt: null,
    output: [],
    received: 0,
    finished: null,
    live: false,
    liveCut: false,
  };
}

const str = (value: unknown): string | null => (typeof value === "string" ? value : null);
const num = (value: unknown): number | null => (typeof value === "number" ? value : null);

function appendText(output: CodeRunSegment[], stream: CodeRunStream, text: string) {
  const last = output.at(-1);
  if (last?.stream === stream) return [...output.slice(0, -1), { stream, text: last.text + text }];
  return [...output, { stream, text }];
}

/**
 * Append a live output batch, dropping what the run already holds by offset.
 * Like the server's saved copy, at most {@link CODE_RUN_OUTPUT_MAX_CHARS} are kept.
 * Returns *run* itself when nothing changes, so subscribers don't re-render.
 */
function applyOutput(run: CodeRun, offset: number, stream: CodeRunStream, text: string): CodeRun {
  const end = offset + text.length;
  if (run.finished || end <= run.received) return run.live ? run : { ...run, live: true };
  const fresh = text.slice(Math.max(0, run.received - offset));
  const kept = fresh.slice(0, Math.max(0, CODE_RUN_OUTPUT_MAX_CHARS - run.received));
  return {
    ...run,
    output: kept ? appendText(run.output, stream, kept) : run.output,
    received: end,
    live: true,
    liveCut: run.liveCut || kept.length < fresh.length,
  };
}

interface QueuedOutput {
  sessionId: string;
  runId: string;
  offset: number;
  stream: CodeRunStream;
  text: string;
}

const nextFrame = (callback: () => void): void => {
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(callback);
  else setTimeout(callback, 16);
};

/** Merge a saved `code_run` item (either phase) into its run. */
function applyItem(run: CodeRun, item: WireItem): CodeRun {
  if (item.phase === "started") {
    return {
      ...run,
      messageId: str(item.message_id) ?? run.messageId,
      blockHash: str(item.block_hash) ?? run.blockHash,
      language: (str(item.language) as CodeRunLanguage | null) ?? run.language,
      command: str(item.command) ?? run.command,
      startedAt: num(item.started_at) ?? run.startedAt,
    };
  }
  const output = readCodeRunSegments(item.output);
  return {
    ...run,
    output,
    received: output.reduce((total, segment) => total + segment.text.length, 0),
    finished: {
      status: (str(item.status) as CodeRunFinishedStatus | null) ?? "failed",
      exitCode: num(item.exit_code),
      durationMs: num(item.duration_ms) ?? 0,
      truncated: item.truncated === true,
    },
  };
}

/**
 * Live and saved runs of chat code blocks, by session then run id.
 *
 * Fed by the session stream (`session.code_run.*`) and by every loaded page
 * of session items, so a run's panel survives a reload and a reconnect.
 */
export const useCodeRunStore = create<CodeRunStoreState>((set) => {
  const updateSession = (
    sessionId: string,
    update: (session: SessionCodeRuns) => Partial<SessionCodeRuns>,
  ) =>
    set((state) => {
      const session = state.sessions[sessionId] ?? EMPTY_SESSION;
      return { sessions: { ...state.sessions, [sessionId]: { ...session, ...update(session) } } };
    });
  const updateRun = (sessionId: string, runId: string, update: (run: CodeRun) => CodeRun) =>
    updateSession(sessionId, (session) => ({
      runs: { ...session.runs, [runId]: update(session.runs[runId] ?? emptyRun(runId)) },
    }));
  const applyOutputs = (entries: readonly QueuedOutput[]) =>
    set((state) => {
      let sessions = state.sessions;
      for (const { sessionId, runId, offset, stream, text } of entries) {
        const session = sessions[sessionId] ?? EMPTY_SESSION;
        const run = session.runs[runId] ?? emptyRun(runId);
        const next = applyOutput(run, offset, stream, text);
        if (next === run) continue;
        sessions = {
          ...sessions,
          [sessionId]: { ...session, runs: { ...session.runs, [runId]: next } },
        };
      }
      return sessions === state.sessions ? state : { sessions };
    });
  let queued: QueuedOutput[] = [];
  const flushQueued = () => {
    const entries = queued;
    queued = [];
    applyOutputs(entries);
  };
  const upsertItem = (sessionId: string, item: WireItem, live: boolean) => {
    const runId = str(item.run_id);
    if (!runId) return;
    updateRun(sessionId, runId, (run) => ({ ...applyItem(run, item), live: run.live || live }));
  };

  return {
    sessions: {},
    // One pass and one store update for a whole page of history.
    ingestItems: (sessionId, items) => {
      const codeRuns = items.filter(
        (item): item is WireItem =>
          (item as WireItem)?.type === "code_run" && str((item as WireItem).run_id) !== null,
      );
      if (codeRuns.length === 0) return;
      updateSession(sessionId, (session) => {
        const runs = { ...session.runs };
        for (const item of codeRuns) {
          const runId = item.run_id as string;
          runs[runId] = applyItem(runs[runId] ?? emptyRun(runId), item);
        }
        return { runs };
      });
    },
    started: (sessionId, item) => upsertItem(sessionId, item, true),
    finished: (sessionId, item) => upsertItem(sessionId, item, false),
    output: (sessionId, runId, offset, stream, text) =>
      applyOutputs([{ sessionId, runId, offset, stream, text }]),
    queueOutput: (sessionId, runId, offset, stream, text) => {
      if (queued.length === 0) nextFrame(flushQueued);
      queued.push({ sessionId, runId, offset, stream, text });
    },
    snapshot: (sessionId, runId, offset, output) =>
      updateRun(sessionId, runId, (run) =>
        run.finished || offset <= run.received
          ? { ...run, live: true }
          : { ...run, output, received: offset, live: true },
      ),
    accepted: (sessionId, runId, accepted) =>
      updateRun(sessionId, runId, (run) => ({
        ...run,
        messageId: run.messageId ?? accepted.messageId,
        blockHash: run.blockHash ?? accepted.blockHash,
        language: run.language ?? accepted.language,
        command: run.command || accepted.command,
        startedAt: run.startedAt ?? Math.floor(Date.now() / 1000),
        live: true,
      })),
    streamOpened: (sessionId) =>
      updateSession(sessionId, (session) => ({
        listingSettled: false,
        runs: Object.fromEntries(
          Object.entries(session.runs).map(([runId, run]) => [runId, { ...run, live: false }]),
        ),
      })),
    listingSettled: (sessionId) => updateSession(sessionId, () => ({ listingSettled: true })),
    setRefusal: (sessionId, blockKey, reason) =>
      updateSession(sessionId, (session) => {
        const { [blockKey]: _dropped, ...refusals } = session.refusals;
        return { refusals: reason === null ? refusals : { ...refusals, [blockKey]: reason } };
      }),
    setCopyOnWrite: (sessionId) => updateSession(sessionId, () => ({ copyOnWrite: true })),
  };
});

/**
 * What a run's status line shows. A run without its `finished` item is
 * running while the runner reports it, interrupted once the runner is
 * connected and doesn't list it, and waiting while the runner is offline.
 */
export function codeRunState(
  session: Pick<SessionCodeRuns, "listingSettled">,
  run: CodeRun,
  runnerOnline: boolean,
): CodeRunState {
  if (run.finished) return run.finished.status;
  if (!runnerOnline) return "waiting";
  if (run.live || !session.listingSettled) return "running";
  return "interrupted";
}

/** A block's runs, newest first. */
export function blockRuns(
  session: SessionCodeRuns | undefined,
  messageId: string,
  blockHash: string,
): CodeRun[] {
  if (!session) return [];
  // Reversed first so runs started in the same second keep newest-first order.
  return Object.values(session.runs)
    .reverse()
    .filter((run) => run.messageId === messageId && run.blockHash === blockHash)
    .sort((a, b) => (b.startedAt ?? 0) - (a.startedAt ?? 0));
}

/**
 * A block's run ids, newest first. Selectors return this with shallow equality,
 * so a block re-renders only when its own list of runs changes.
 */
export function blockRunIds(
  session: SessionCodeRuns | undefined,
  messageId: string,
  blockHash: string,
): string[] {
  return blockRuns(session, messageId, blockHash).map((run) => run.runId);
}

/** Key for a block's local-only state: message id plus block hash. */
export function codeBlockKey(messageId: string, blockHash: string): string {
  return `${messageId}:${blockHash}`;
}
