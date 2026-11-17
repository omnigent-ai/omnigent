// Keeps the browser terminal from answering a pane program's query when tmux,
// the pane's real terminal, has already answered it. Control mode forwards the
// raw query here too, so a second answer would reach the program as typed input.

import type { IDisposable, Terminal } from "@xterm/xterm";

/** CSI parameters as xterm.js hands them to a registered handler. */
type CsiParams = (number | number[])[];

/**
 * Parse the runner's list of the queries tmux answers itself.
 *
 * :param message: A text frame from the terminal WebSocket, e.g.
 *     ``{"type":"tmux-replies","queries":["da1","dsr:6"]}``.
 * :returns: The query identifiers, or ``null`` for any other message.
 */
export function parseTmuxRepliesMessage(message: string): ReadonlySet<string> | null {
  let value: unknown;
  try {
    value = JSON.parse(message);
  } catch {
    return null;
  }
  if (typeof value !== "object" || value === null) return null;
  const { type, queries } = value as { type?: unknown; queries?: unknown };
  if (type !== "tmux-replies" || !Array.isArray(queries)) return null;
  if (!queries.every((query) => typeof query === "string")) return null;
  return new Set(queries as string[]);
}

/** The first parameter of a CSI sequence; an omitted one reads as 0. */
function firstParam(params: CsiParams): number {
  const first = params[0];
  return typeof first === "number" ? first : 0;
}

/**
 * Consume the queries tmux answers before xterm.js can reply to them.
 *
 * Every other query falls through to xterm.js, so a program still gets exactly
 * one answer to anything it asks. Queries are consumed in the parser rather
 * than filtered out of ``onData`` because a row-1 cursor report and Shift+F3
 * are the same bytes.
 *
 * :param term: The terminal rendering a tmux pane.
 * :param tmuxAnswers: Identifiers from ``omnigent/terminals/query_replies.py``,
 *     e.g. ``"dsr:6"`` or ``"decrqm:?*"``, where ``*`` means every request of
 *     that kind.
 * :returns: A disposable that removes the handlers.
 */
export function suppressRepliesTmuxSends(
  term: Terminal,
  tmuxAnswers: ReadonlySet<string>,
): IDisposable {
  const parser = term.parser;
  const answers = (query: string) => tmuxAnswers.has(query);
  const answersMode = (kind: string, params: CsiParams) =>
    answers(`${kind}*`) || answers(`${kind}${firstParam(params)}`);

  // The probe asks for one colour per query, and tmux leaves combined forms
  // such as "10;?;?" unanswered, so only a single-colour query is consumed.
  const isPaletteQuery = (data: string) => /^\d+;\?$/.test(data);

  const handlers: IDisposable[] = [
    parser.registerCsiHandler(
      { final: "c" },
      (params) => firstParam(params) === 0 && answers("da1"),
    ),
    parser.registerCsiHandler(
      { prefix: ">", final: "c" },
      (params) => firstParam(params) === 0 && answers("da2"),
    ),
    parser.registerCsiHandler({ final: "n" }, (params) => answers(`dsr:${firstParam(params)}`)),
    parser.registerCsiHandler({ prefix: "?", final: "n" }, (params) =>
      answers(`dsr:?${firstParam(params)}`),
    ),
    parser.registerCsiHandler({ intermediates: "$", final: "p" }, (params) =>
      answersMode("decrqm:", params),
    ),
    parser.registerCsiHandler({ prefix: "?", intermediates: "$", final: "p" }, (params) =>
      answersMode("decrqm:?", params),
    ),
    parser.registerDcsHandler(
      { intermediates: "$", final: "q" },
      (data) => answers("decrqss:*") || answers(`decrqss:${data}`),
    ),
    parser.registerOscHandler(4, (data) => isPaletteQuery(data) && answers("osc:4")),
    ...[10, 11, 12].map((code) =>
      parser.registerOscHandler(code, (data) => data === "?" && answers(`osc:${code}`)),
    ),
  ];

  return {
    dispose: () => {
      for (const handler of handlers) handler.dispose();
    },
  };
}
