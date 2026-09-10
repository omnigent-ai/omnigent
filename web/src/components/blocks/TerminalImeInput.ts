import type { Terminal } from "@xterm/xterm";
import {
  normalizeTerminalTextareaSelection,
  terminalTextareaEdit,
  type TerminalTextareaSnapshot,
} from "./terminalTextareaEdit";

interface PendingEdit {
  kind: "key" | "input" | "composition";
  before: TerminalTextareaSnapshot;
  data: string | null;
  after: TerminalTextareaSnapshot | null;
  settled: boolean;
}

const EDIT_INPUT_TYPES = new Set([
  "insertText",
  "insertReplacementText",
  "deleteContentBackward",
  "deleteContentForward",
  "deleteWordBackward",
  "deleteWordForward",
]);

/** Reconcile mobile DOM edits without treating the IME's caret as the line end. */
export class TerminalImeInput {
  private readonly term: Terminal;
  private readonly listeners = new AbortController();
  private readonly keyListener: { dispose: () => void };
  private pending: PendingEdit | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private nativeKey = false;
  private pasting = false;
  private composing = false;
  private forwarding = false;

  constructor(term: Terminal) {
    this.term = term;
    const { signal } = this.listeners;
    const textarea = term.textarea;
    const root = term.element;
    // Root capture runs before xterm's textarea-capture input handler.
    root?.addEventListener("beforeinput", this.beforeInput, { capture: true, signal });
    root?.addEventListener("input", this.input, { capture: true, signal });
    root?.addEventListener("paste", this.paste, { capture: true, signal });
    root?.addEventListener("blur", this.blur, { capture: true, signal });
    textarea?.addEventListener("compositionstart", this.compositionStart, { signal });
    textarea?.addEventListener("compositionend", this.compositionEnd, { signal });
    this.keyListener = term.onKey(() => {
      // xterm finalizes its composition before raising onKey for the next key.
      if (!this.composing) this.flush(true, this.pending?.after ?? undefined);
    });
  }

  get isComposing(): boolean {
    return this.composing;
  }

  private get enabled(): boolean {
    return navigator.maxTouchPoints > 0 && !this.term.options.screenReaderMode;
  }

  private snapshot(): TerminalTextareaSnapshot {
    const textarea = this.term.textarea!;
    return {
      value: textarea.value,
      selectionStart: textarea.selectionStart,
      selectionEnd: textarea.selectionEnd,
    };
  }

  /** Return false only when the browser IME owns this keyboard event. */
  handleKeyEvent(event: KeyboardEvent): boolean {
    if (!this.enabled) return true;
    if (this.composing || event.isComposing) return false;
    const processing = event.keyCode === 229 || event.key === "Process";
    if (processing) {
      this.nativeKey = false;
      if (event.type === "keydown" && !this.pending) this.start("key");
      if (event.type === "keyup" && this.pending?.kind === "key") this.flush(false);
      return false;
    }
    if (event.type === "keyup") {
      this.nativeKey = false;
      if (this.pending?.kind === "key") {
        this.flush(false);
        return false;
      }
    } else if (event.type === "keydown") {
      if (this.pending?.kind !== "composition") this.flush();
      else this.pending.after = this.snapshot();
      this.nativeKey = true;
    }
    return true;
  }

  /** Native composition still renders preedit, but its end-of-line diff is not sent. */
  consumeData(data: string): boolean {
    if (this.forwarding || !this.enabled || this.pending?.kind !== "composition" || this.pasting)
      return false;
    // Terminal replies and explicit soft keys are not IME commit text.
    const code = data.charCodeAt(0);
    if (data.startsWith("\x1b") || (data.length === 1 && (code < 32 || code === 127))) return false;
    return true;
  }

  beforeSoftKey(): boolean {
    if (!this.enabled) return true;
    if (this.composing) return false;
    this.flush(true, this.snapshot(), true);
    return true;
  }

  private send(data: string): void {
    this.forwarding = true;
    try {
      // Keep xterm's scroll, selection, input-disable and activity semantics.
      this.term.input(data, true);
    } finally {
      this.forwarding = false;
    }
  }

  private start(kind: PendingEdit["kind"]): void {
    this.pending = {
      kind,
      before: this.snapshot(),
      data: null,
      after: null,
      settled: false,
    };
    if (kind !== "composition") this.schedule(80);
  }

  private schedule(delay: number): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.flush();
    }, delay);
  }

  private flush(clearEmpty = true, observed = this.snapshot(), keepComposition = false): void {
    if (this.composing) return;
    const pending = this.pending;
    if (!pending || !this.enabled) {
      this.clear();
      return;
    }
    if (pending.settled) {
      if (!keepComposition) this.clear();
      return;
    }
    const after = normalizeTerminalTextareaSelection(pending.before, observed, pending.data);
    const data = terminalTextareaEdit(
      pending.before,
      after,
      this.term.modes.applicationCursorKeysMode,
    );
    if (!data && !clearEmpty) return;
    if (keepComposition && pending.kind === "composition") pending.settled = true;
    else this.clear();
    if (
      after.selectionStart !== observed.selectionStart ||
      after.selectionEnd !== observed.selectionEnd
    ) {
      this.term.textarea!.setSelectionRange(after.selectionStart, after.selectionEnd);
    }
    if (data) this.send(data);
  }

  private clear(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.pending = null;
  }

  private compositionStart = (): void => {
    if (this.enabled) {
      this.flush();
      this.start("composition");
    }
    this.nativeKey = false;
    this.composing = true;
  };

  private compositionEnd = (event: CompositionEvent): void => {
    this.composing = false;
    if (this.pending?.kind === "composition") {
      this.pending.data = event.data;
      this.pending.after = this.snapshot();
      // Registered after xterm's compositionend, so its final callback runs first.
      this.schedule(0);
    }
  };

  private beforeInput = (event: InputEvent): void => {
    if (event.target !== this.term.textarea || !this.enabled || this.composing || event.isComposing)
      return;
    if (!EDIT_INPUT_TYPES.has(event.inputType)) return;
    if (this.nativeKey && event.inputType === "insertText") return;
    if (!this.pending) this.start("input");
  };

  private input = (event: InputEvent): void => {
    if (event.target !== this.term.textarea || !this.enabled) return;
    if (this.pending) {
      event.stopPropagation();
      this.pending.data = event.data;
      this.pending.after = this.snapshot();
      if (!this.composing && this.pending.kind !== "composition") {
        this.schedule(this.pending.kind === "key" ? 80 : 0);
      }
      return;
    }
    if (!this.nativeKey && !event.isComposing && event.inputType === "insertText" && event.data) {
      // Some keyboards emit neither keydown nor beforeinput for committed symbols.
      event.stopPropagation();
      this.send(event.data);
    }
  };

  private paste = (): void => {
    if (!this.enabled) return;
    this.flush(true, this.snapshot(), true);
    if (this.pending) this.pending.settled = true;
    this.pasting = true;
    queueMicrotask(() => {
      this.pasting = false;
    });
  };

  private blur = (event: FocusEvent): void => {
    if (event.target !== this.term.textarea || !this.enabled) return;
    this.nativeKey = false;
    if (this.composing) {
      this.composing = false;
      if (this.pending) this.pending.settled = true;
      this.schedule(0);
      return;
    }
    // xterm clears the textarea in its blur listener; settle before that happens.
    this.flush(true, this.snapshot(), true);
  };

  dispose(): void {
    this.listeners.abort();
    this.keyListener.dispose();
    this.clear();
    this.composing = false;
  }
}
