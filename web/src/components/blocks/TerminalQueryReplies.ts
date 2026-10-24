// Keeps the browser terminal from answering the queries a pane program writes.
// tmux, the pane's real terminal, already answers them, and control mode forwards
// the raw query here too, so a second answer would reach the program as typed input.

import type { IDisposable, Terminal } from "@xterm/xterm";

/**
 * Consume the terminal queries tmux answers before xterm.js can reply to them.
 *
 * A plain ``tmux attach`` never shows its outer terminal the pane's queries, so
 * answering none of them here gives programs what they get there; otherwise
 * PowerShell, which asks for the cursor position on every prompt, shows the
 * extra reply as ``28;1R``. The queries are consumed in the parser rather than
 * filtered out of ``onData`` because a row-1 cursor report and Shift+F3 are the
 * same bytes.
 *
 * :param term: The terminal rendering a tmux pane.
 * :returns: A disposable that removes the handlers.
 */
export function suppressTerminalQueryReplies(term: Terminal): IDisposable {
  const parser = term.parser;
  const handled = () => true;
  // OSC 4/10/11/12 both set and query colours; only a "?" asks for a reply.
  const isColourQuery = (data: string) => data.includes("?");

  const handlers: IDisposable[] = [
    parser.registerCsiHandler({ final: "c" }, handled), // primary device attributes
    parser.registerCsiHandler({ prefix: ">", final: "c" }, handled), // secondary device attributes
    parser.registerCsiHandler({ final: "n" }, handled), // device status, cursor position
    parser.registerCsiHandler({ prefix: "?", final: "n" }, handled), // extended cursor position
    parser.registerCsiHandler({ intermediates: "$", final: "p" }, handled), // ANSI mode report
    parser.registerCsiHandler({ prefix: "?", intermediates: "$", final: "p" }, handled), // DEC mode report
    parser.registerDcsHandler({ intermediates: "$", final: "q" }, handled), // status string
    ...[4, 10, 11, 12].map((code) => parser.registerOscHandler(code, isColourQuery)),
  ];

  return {
    dispose: () => {
      for (const handler of handlers) handler.dispose();
    },
  };
}
