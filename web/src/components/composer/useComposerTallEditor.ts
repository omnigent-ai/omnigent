// A message box that can trade its growing input for a tall editor: its height for `scope`,
// the corner toggle once the draft overflows, the ⌘K row and chord, and a long paste into an
// empty box expanding it.

import { useEffect, useRef, type ClipboardEvent, type RefObject } from "react";

import { useComposerInputOverflow } from "@/components/composer/useComposerInputOverflow";
import { useComposerHeightHotkey } from "@/hooks/useComposerHeightHotkey";
import {
  pasteExpandsComposer,
  setComposerHeight,
  toggleComposerHeight,
  useComposerHeight,
} from "@/lib/composerHeightState";

export interface ComposerTallEditor {
  /** The box shows the tall editor. */
  tall: boolean;
  /** The corner toggle shows: the draft has outgrown the box, or the box is tall. */
  showToggle: boolean;
  toggle: () => void;
  /** Watches pastes, so a long one into an empty box expands it once its text lands. */
  onPasteCapture: (event: ClipboardEvent<HTMLElement>) => void;
}

/**
 * The tall editor for the box showing `scope`'s height, while `enabled`. With no scope, or
 * disabled, the box stays as it was: no toggle, no ⌘K row and no chord.
 */
export function useComposerTallEditor({
  scope,
  enabled,
  rootRef,
  inputAreaRef,
  draftText,
  hasInputPrefix,
}: {
  scope: string | undefined;
  enabled: boolean;
  /** The composer card, where focus entering makes it the box ⌘K acts on. */
  rootRef: RefObject<HTMLElement | null>;
  /** The area around the inputs, measured for overflow. */
  inputAreaRef: RefObject<HTMLElement | null>;
  draftText: string;
  /** Quote blocks sit above the draft, so the box isn't empty for a paste. */
  hasInputPrefix: boolean;
}): ComposerTallEditor {
  const active = enabled && scope !== undefined;
  const heightScope = scope ?? "";
  const height = useComposerHeight(heightScope);
  useComposerHeightHotkey({ scope: heightScope, enabled: active, root: rootRef });
  const tall = active && height === "expanded";
  const overflowing = useComposerInputOverflow(inputAreaRef, active && !tall);

  // A long paste expands the box only once its text lands; one that becomes an attachment
  // leaves the draft empty, and the box as it was.
  // Held as the scope it was pasted into, so a session switch or a box that can no longer
  // expand drops it.
  const pendingExpand = useRef<string | null>(null);
  useEffect(() => {
    const pastedInto = pendingExpand.current;
    if (pastedInto === null) return;
    if (!active || pastedInto !== heightScope) {
      pendingExpand.current = null;
      return;
    }
    if (draftText === "") return;
    pendingExpand.current = null;
    if (pasteExpandsComposer(draftText)) setComposerHeight(pastedInto, "expanded");
  }, [active, draftText, heightScope]);

  const onPasteCapture = (event: ClipboardEvent<HTMLElement>) => {
    if (!active || tall || draftText !== "" || hasInputPrefix) return;
    const pasted = event.clipboardData?.getData("text/plain").replace(/\r\n?/g, "\n") ?? "";
    if (!pasteExpandsComposer(pasted)) return;
    pendingExpand.current = heightScope;
    // Nothing landing by the next frame means the paste went elsewhere.
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        pendingExpand.current = null;
      }),
    );
  };

  return {
    tall,
    showToggle: tall || (active && overflowing),
    toggle: () => toggleComposerHeight(heightScope),
    onPasteCapture,
  };
}
