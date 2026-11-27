// A Markdown textarea's `@` and `#` completion: the token the caret ends, the suggestions for it,
// which one is active, and the keys that move and pick while the menu shows. Picking writes
// through the textarea's own undo history; everything else stays the editor's.

import { type RefObject, useCallback, useMemo, useState } from "react";

import type { Person } from "@/lib/peopleSearch";

import {
  type CompletionToken,
  completionEdit,
  completionText,
  completionTokenAt,
  type PullRequestCandidate,
  rankPullRequests,
} from "./markdownCompletion";
import { useMarkdownCompletionSource } from "./markdownCompletionContext";
import { applyUndoableTextEdit, textareaEdit } from "./markdownFormatting";
import { usePullRequestCompletions } from "./pullRequestCompletionSearch";
import { useCompletionPeople } from "./useCompletionPeople";

export type MarkdownCompletionItem =
  | { key: string; kind: "person"; person: Person }
  | { key: string; kind: "pullRequest"; pullRequest: PullRequestCandidate };

export interface MarkdownCompletion {
  /** The token being completed while the menu shows; null while it's closed. */
  token: CompletionToken | null;
  items: readonly MarkdownCompletionItem[];
  /** Suggestions are still being read, so an empty list isn't "nothing found" yet. */
  loading: boolean;
  /** The active item's key. */
  active: string;
  setActive: (key: string) => void;
  pick: (item: MarkdownCompletionItem) => void;
  /** Read the token at the caret again, after typing, a click or a caret move. */
  refresh: () => void;
  /** Close the menu, as Escape, a blur or Preview does. */
  dismiss: () => void;
  /** Handle a key while the menu shows; true when completion took it. */
  handleKey: (event: KeyboardEvent) => boolean;
}

const sameToken = (left: CompletionToken | null, right: CompletionToken | null) =>
  left?.start === right?.start && left?.kind === right?.kind;

export function useMarkdownCompletion(
  textareaRef: RefObject<HTMLTextAreaElement | null>,
): MarkdownCompletion {
  const source = useMarkdownCompletionSource();
  const [token, setToken] = useState<CompletionToken | null>(null);
  // The token Escape closed: it stays closed until the caret leaves it.
  const [dismissed, setDismissed] = useState<CompletionToken | null>(null);
  const [active, setActive] = useState("");

  const refresh = useCallback(() => {
    const textarea = textareaRef.current;
    if (!source || !textarea || textarea.selectionStart !== textarea.selectionEnd) {
      setToken(null);
      return;
    }
    const next = completionTokenAt(textarea.value, textarea.selectionStart);
    setDismissed((current) => (current && sameToken(current, next) ? current : null));
    setToken((current) =>
      current && next && sameToken(current, next) && current.query === next.query ? current : next,
    );
  }, [source, textareaRef]);

  const shown = token && !(dismissed && sameToken(dismissed, token)) ? token : null;
  const people = useCompletionPeople(source, shown?.kind === "person" ? shown.query : null);
  const typedNumberOrWords = shown?.kind === "pullRequest" ? shown.query : null;
  const searched = usePullRequestCompletions(
    source?.prUrl ?? null,
    typedNumberOrWords,
    source?.conversationId ?? "",
  );

  const items = useMemo((): MarkdownCompletionItem[] => {
    if (!shown) return [];
    if (shown.kind === "person") {
      return people.people.map((person) => ({
        key: `person:${person.kind}:${person.login.toLowerCase()}`,
        kind: "person",
        person,
      }));
    }
    // The PRs the view already read come first; the search adds the rest it finds.
    const candidates = [...(source?.pullRequests ?? []), ...searched.pullRequests];
    return rankPullRequests(candidates, shown.query).map((pullRequest) => ({
      key: `pull-request:${pullRequest.url}`,
      kind: "pullRequest",
      pullRequest,
    }));
  }, [people.people, searched.pullRequests, shown, source?.pullRequests]);

  const loading = shown?.kind === "person" ? people.pending : searched.searching;
  // Shown while it lists something, reads, or says a typed query found nothing.
  const open = !!shown && (items.length > 0 || loading || shown.query !== "");
  // The first item is active until the arrows pick another that's still listed.
  const activeKey = items.some((item) => item.key === active) ? active : (items[0]?.key ?? "");

  const dismiss = useCallback(() => setDismissed(token), [token]);

  const pick = useCallback(
    (item: MarkdownCompletionItem) => {
      const textarea = textareaRef.current;
      if (!textarea || !shown) return;
      // The token as the textarea has it now: a pick never writes over text typed since.
      const current = completionTokenAt(textarea.value, textarea.selectionStart);
      if (!current || !sameToken(current, shown)) return;
      const text = completionText(
        item.kind === "person"
          ? { kind: "person", person: item.person }
          : { kind: "pullRequest", pullRequest: item.pullRequest },
      );
      applyUndoableTextEdit(textarea, completionEdit(textareaEdit(textarea), current, text));
      setToken(null);
      setActive("");
    },
    [shown, textareaRef],
  );

  const handleKey = useCallback(
    (event: KeyboardEvent): boolean => {
      if (!open || event.isComposing) return false;
      // Only unmodified keys: ⌘↵ still submits, ⌘B still formats, ⌥↓ still moves.
      if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return false;
      if (event.key === "Escape") {
        dismiss();
        return true;
      }
      if (items.length === 0) return false;
      const index = Math.max(
        0,
        items.findIndex((item) => item.key === activeKey),
      );
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        const step = event.key === "ArrowDown" ? 1 : -1;
        setActive(items[(index + step + items.length) % items.length]!.key);
        return true;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        pick(items[index]!);
        return true;
      }
      return false;
    },
    [activeKey, dismiss, items, open, pick],
  );

  return {
    token: open ? shown : null,
    items,
    loading,
    active: activeKey,
    setActive,
    pick,
    refresh,
    dismiss,
    handleKey,
  };
}
