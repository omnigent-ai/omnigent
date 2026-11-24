// The tabs sidebar asks before a shell's tab closes, then ends the shell itself. Each
// request settles only itself: a newer one cancels the older, and the dialog answers only
// the request it shows.

import { useCallback, useEffect, useRef, useState } from "react";

import { terminalDisplayLabel, terminalTabKey } from "@/hooks/useTerminals";
import type { TerminalInfo } from "@/lib/terminals";

type Answer = (closed: boolean) => void;

/** Shells named as the close dialog and its failures name them: "a", "a and b", "a, b and c". */
export function shellLabelList(labels: readonly string[]): string {
  return labels.length > 1 ? `${labels.slice(0, -1).join(", ")} and ${labels.at(-1)}` : labels[0];
}

/** The shell close the dialog is asking about, and the request it answers. */
export interface TabsShellCloseQuestion {
  /** The shells' labels, as the dialog names them: "a", or "a, b and c". */
  label: string;
  /** How many shells closing ends; more than one for a split's close. */
  count: number;
  answer: Answer;
}

export interface TabsShellClose {
  /** Ask before `key`'s tab closes; true once it may end, false when kept or superseded. */
  confirmCloseShell: (key: string) => Promise<boolean>;
  /** Ask once before a split's shells close together; true once they may end. */
  confirmCloseShells: (keys: readonly string[]) => Promise<boolean>;
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
  /** Ends a terminal; rejects when it couldn't, leaving the sidebar to tell the user. */
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

  const confirmCloseShells = useCallback(
    (keys: readonly string[]) => {
      // A newer close supersedes the one still asking, which keeps its shell.
      if (pending.current) settle(pending.current, false);
      // A shell that's already gone has nothing left to end.
      const labels = keys.flatMap((key) => {
        const terminal = findTerminal(key);
        return terminal ? [terminalDisplayLabel(terminal)] : [];
      });
      if (labels.length === 0) return Promise.resolve(true);
      const label = shellLabelList(labels);
      return new Promise<boolean>((resolve) => {
        pending.current = resolve;
        setQuestion({ label, count: labels.length, answer: resolve });
      });
    },
    [settle, findTerminal],
  );
  const confirmCloseShell = useCallback(
    (key: string) => confirmCloseShells([key]),
    [confirmCloseShells],
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

  return { confirmCloseShell, confirmCloseShells, endShell, question, answerQuestion };
}
