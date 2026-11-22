// Large text pastes as attachments, for one composer owner. The rules and the memory of each
// paste are the ledger, which loads on its own once the CodeMirror editor has, since only that
// engine turns a paste into an attachment. Until then nothing can have been pasted, so this
// reports nothing and leaves every paste to the input.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { useCodeMirrorComposerLoad } from "@/components/composer/engine/composerEnginePolicy";
import {
  COMPOSER_INPUT_ID_ATTRIBUTE,
  COMPOSER_TAIL_INPUT_ID,
  composerTargetElement,
  type ComposerPasteEvent,
  type ComposerTextTarget,
} from "@/components/composer/engine/composerInput";
import type {
  PastedTextChip,
  PastedTextContext,
  PastedTextEnvironment,
  PastedTextLedger,
  PastedTextSnapshot,
} from "@/components/composer/engine/pastedTextLedger";
import { insertComposerText } from "@/lib/composerText";

export type { PastedTextChip } from "@/components/composer/engine/pastedTextLedger";

export interface PastedTextAttachmentsOptions extends PastedTextContext {
  /** The draft the pastes belong to; a new one forgets them. */
  draftKey: string | null;
}

export interface PastedTextHint extends PastedTextChip {
  /** ⌘Z right now turns the newest paste back into text; anything since ends that. */
  undoable: boolean;
}

export interface PastedTextAttachments {
  /**
   * Take a paste into `target` as an attachment when its text is large. Returns whether it
   * handled the paste, accepted or not, so the caller claims the event.
   */
  offerPaste: (target: ComposerTextTarget, data: ComposerPasteEvent["clipboardData"]) => boolean;
  /** Put a pasted attachment's text back into the draft at its place, and remove the chip. */
  insertAsText: (file: File) => boolean;
  /** ⌘Z right after a paste became an attachment; false when it isn't that moment. */
  undoLatestPaste: () => boolean;
  /**
   * Anything since the paste (an edit in any input, a selection move, an engine switch, a
   * removed quote) ends ⌘Z's restore; Insert as text keeps working.
   */
  endUndo: () => void;
  /** Every paste still attached, oldest first, each restorable on its own. */
  chips: readonly PastedTextChip[];
  /** Whether a restore can run now; a locked draft keeps its chips until it unlocks. */
  canRestore: boolean;
  /** The newest paste, for the hint under the chips. */
  hint: PastedTextHint | null;
}

type LedgerFactory = () => PastedTextLedger;

/** What the ledger takes from the app. */
export function pastedTextEnvironment(): PastedTextEnvironment {
  return {
    tailInputId: COMPOSER_TAIL_INPUT_ID,
    inputIdOf: (target) => composerTargetElement(target).getAttribute(COMPOSER_INPUT_ID_ATTRIBUTE),
    insertText: (target, text, start, end) => insertComposerText(target, text, start, end),
  };
}

// The ledger's module, shared by every owner once loaded; a failed load is tried again later.
let loadedFactory: LedgerFactory | null = null;
let pendingFactory: Promise<LedgerFactory> | null = null;

/** Load the ledger's module ahead of a paste; owners also load it once the editor has. */
export function loadPastedTextLedger(): Promise<LedgerFactory> {
  pendingFactory ??= import("@/components/composer/engine/pastedTextLedger").then(
    (module) => {
      loadedFactory = () => module.createPastedTextLedger(pastedTextEnvironment());
      return loadedFactory;
    },
    (error: unknown) => {
      pendingFactory = null;
      throw error;
    },
  );
  return pendingFactory;
}

/** The ledger factory once both the CodeMirror editor and the ledger's own module have loaded. */
function useLedgerFactory(): LedgerFactory | null {
  const editorReady = useCodeMirrorComposerLoad().status === "ready";
  const [factory, setFactory] = useState<LedgerFactory | null>(() => loadedFactory);
  useEffect(() => {
    if (!editorReady || factory) return;
    let live = true;
    loadPastedTextLedger().then(
      (loaded) => {
        if (live) setFactory(() => loaded);
      },
      (error: unknown) => {
        console.warn("The large-paste ledger couldn't load; pastes stay inline.", error);
      },
    );
    return () => {
      live = false;
    };
  }, [editorReady, factory]);
  return editorReady ? factory : null;
}

const NOTHING_PASTED: PastedTextSnapshot = { chips: [], undoable: false };
const noSubscription = () => () => {};

export function usePastedTextAttachments({
  draftKey,
  ...context
}: PastedTextAttachmentsOptions): PastedTextAttachments {
  const createLedger = useLedgerFactory();
  const ledger = useMemo<PastedTextLedger | null>(() => createLedger?.() ?? null, [createLedger]);
  const latest = useRef(context);
  latest.current = context;

  const snapshot = useSyncExternalStore(
    ledger?.subscribe ?? noSubscription,
    ledger?.snapshot ?? (() => NOTHING_PASTED),
  );

  // A new draft starts with nothing to restore.
  useLayoutEffect(() => {
    ledger?.reset();
  }, [ledger, draftKey]);
  // Follow the owner's edits and attachment changes; `valueOf` changes with the draft.
  const { files, valueOf } = context;
  useLayoutEffect(() => {
    ledger?.sync(latest.current);
  }, [ledger, files, valueOf]);
  // A restore waiting for its input to show finishes on the render that mounts it.
  useLayoutEffect(() => {
    ledger?.completePendingRestore(latest.current);
  });

  const insertAsText = useCallback(
    (file: File) => ledger?.insertAsText(file, latest.current) ?? false,
    [ledger],
  );
  const newest = snapshot.chips.at(-1);
  return {
    offerPaste: useCallback(
      (target, data) => ledger?.offerPaste(target, data, latest.current) ?? false,
      [ledger],
    ),
    insertAsText,
    undoLatestPaste: useCallback(() => ledger?.undoLatest(latest.current) ?? false, [ledger]),
    endUndo: useCallback(() => ledger?.endUndo(), [ledger]),
    chips: snapshot.chips,
    canRestore: !context.locked,
    hint: newest ? { ...newest, undoable: snapshot.undoable } : null,
  };
}

/** Forget the loaded ledger module, so a test can load it again from scratch. */
export function resetPastedTextLedgerForTests(): void {
  loadedFactory = null;
  pendingFactory = null;
}
