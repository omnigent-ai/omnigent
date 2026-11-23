// The tabs sidebar asks before a shell's tab closes, then ends the shell itself. Each
// request settles only itself: a newer one cancels the older, and the dialog answers only
// the request it shows.

import { useCallback, useEffect, useRef, useState } from "react";

import { terminalDisplayLabel, terminalTabKey } from "@/hooks/useTerminals";
import type { TerminalInfo } from "@/lib/terminals";

type Answer = (closed: boolean) => void;

/** The shell close the dialog is asking about, and the request it answers. */
export interface TabsShellCloseQuestion {
  label: string;
  answer: Answer;
}

export interface TabsShellClose {
  /** Ask before `key`'s tab closes; true once it may end, false when kept or superseded. */
  confirmCloseShell: (key: string) => Promise<boolean>;
  /** End `key`'s shell; false when it couldn't be ended. */
  endShell: (key: string) => Promise<boolean>;
  /** The open dialog's question, if any. */
  question: TabsShellCloseQuestion | null;
  /** The dialog's answer, for the request it shows. */
  answerQuestion: (closed: boolean) => void;
}

export function useTabsShellClose({
  terminals,
  deleteTerminal,
}: {
  terminals: readonly TerminalInfo[];
  /** Ends a terminal; rejects when it couldn't, having told the user. */
  deleteTerminal: (terminalId: string) => Promise<unknown>;
}): TabsShellClose {
  const latestTerminals = useRef(terminals);
  latestTerminals.current = terminals;
  const pending = useRef<Answer | null>(null);
  const [question, setQuestion] = useState<TabsShellCloseQuestion | null>(null);

  const settle = useCallback((answer: Answer, closed: boolean) => {
    if (pending.current !== answer) return;
    pending.current = null;
    setQuestion((current) => (current?.answer === answer ? null : current));
    answer(closed);
  }, []);

  const findTerminal = useCallback(
    (key: string) => latestTerminals.current.find((candidate) => terminalTabKey(candidate) === key),
    [],
  );

  const confirmCloseShell = useCallback(
    (key: string) => {
      // A newer close supersedes the one still asking, which keeps its shell.
      if (pending.current) settle(pending.current, false);
      const terminal = findTerminal(key);
      // A shell that's already gone has nothing left to end.
      if (!terminal) return Promise.resolve(true);
      return new Promise<boolean>((resolve) => {
        pending.current = resolve;
        setQuestion({ label: terminalDisplayLabel(terminal), answer: resolve });
      });
    },
    [settle, findTerminal],
  );

  const endShell = useCallback(
    async (key: string) => {
      const terminal = findTerminal(key);
      if (!terminal) return true;
      try {
        await deleteTerminal(terminal.id);
        return true;
      } catch {
        return false;
      }
    },
    [deleteTerminal, findTerminal],
  );

  const answerQuestion = useCallback(
    (closed: boolean) => {
      if (question) settle(question.answer, closed);
    },
    [question, settle],
  );

  // A close still asking when the sidebar goes keeps its shell.
  useEffect(
    () => () => {
      if (pending.current) settle(pending.current, false);
    },
    [settle],
  );

  return { confirmCloseShell, endShell, question, answerQuestion };
}
