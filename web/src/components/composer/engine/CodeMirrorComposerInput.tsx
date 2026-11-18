// Plain-text CodeMirror adapter with injected composer services.

import { history, historyKeymap, isolateHistory, standardKeymap } from "@codemirror/commands";
import {
  Annotation,
  Compartment,
  EditorSelection,
  EditorState,
  Prec,
  Transaction,
  type Extension,
} from "@codemirror/state";
import {
  EditorView,
  keymap,
  placeholder as placeholderText,
  type KeyBinding,
} from "@codemirror/view";
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  type Ref,
} from "react";

import { cn } from "@/lib/utils";

import type {
  ComposerEditorSurface,
  ComposerInputEvent,
  ComposerInputHeightMode,
  ComposerInputProps,
  ComposerKeyboardEvent,
  ComposerTextTarget,
} from "./composerInput";

/** What the editor takes from the app, handed in by the loader. */
export interface CodeMirrorComposerEnvironment {
  /** The attribute that marks a composer input's focusable node. */
  inputAttribute: string;
  /** The attribute that carries an input's id within its composer. */
  inputIdAttribute: string;
  /** Make the surface findable from its node; returns the undo. */
  registerSurface: (surface: ComposerEditorSurface) => () => void;
}

/** The draft's height cap before it scrolls, matching the textarea's. */
const COMPOSER_MAX_HEIGHT_PX = 180;

/** Marks a transaction that syncs the controlled value in, which isn't a user edit. */
const controlledUpdate = Annotation.define<true>();

/** Whether `binding` claims a chord with both Control and Option, which VoiceOver owns. */
function bindsControlOption(binding: KeyBinding): boolean {
  return [binding.key, binding.mac, binding.win, binding.linux].some(
    (key) => key !== undefined && /(^|-)Ctrl-/.test(key) && /(^|-)Alt-/.test(key),
  );
}

/**
 * CodeMirror's editing keys, minus two kinds. Enter would insert a newline before the composer
 * decides to send; an unclaimed Enter still inserts one through the browser's own input
 * handling. And no Control+Option chord, such as the macOS Emacs keymap's ⌃⌥H, is claimed.
 */
export const COMPOSER_EDITING_KEYMAP: readonly KeyBinding[] = [
  ...historyKeymap,
  ...standardKeymap,
].filter((binding) => binding.key !== "Enter" && !bindsControlOption(binding));

function theme(heightMode: ComposerInputHeightMode): Extension {
  const unbounded = heightMode === "unbounded";
  return EditorView.theme({
    "&": { font: "inherit", color: "inherit", backgroundColor: "transparent" },
    "&.cm-focused": { outline: "none" },
    ".cm-scroller": {
      fontFamily: "inherit",
      lineHeight: "inherit",
      overflowX: "hidden",
      overflowY: unbounded ? "visible" : "auto",
      maxHeight: unbounded ? "none" : `${COMPOSER_MAX_HEIGHT_PX}px`,
      scrollbarWidth: "thin",
      scrollbarColor: "color-mix(in srgb, var(--foreground) 28%, transparent) transparent",
    },
    "&[data-overflowing] .cm-scroller": { overscrollBehaviorY: "contain" },
    // A fade at the top edge says there is more draft above.
    "&[data-scrolled] .cm-scroller": {
      maskImage: "linear-gradient(to bottom, transparent, black 18px)",
    },
    ".cm-content": { padding: "0", caretColor: "currentColor", minHeight: "1lh" },
    ".cm-content:focus-visible": { outline: "none" },
    ".cm-line": { padding: "0" },
    ".cm-placeholder": { color: "var(--muted-foreground)" },
  });
}

function attributes(
  props: ComposerInputProps,
  environment: CodeMirrorComposerEnvironment,
): Extension {
  const locked = props.disabled === true || props.readOnly === true;
  return [
    EditorView.editable.of(props.disabled !== true),
    EditorState.readOnly.of(locked),
    EditorView.contentAttributes.of({
      [environment.inputAttribute]: "",
      "aria-disabled": props.disabled ? "true" : "false",
      ...(props.readOnly ? { "aria-readonly": "true" } : {}),
      // A textarea is named by its placeholder when it has no label; the editor says so itself.
      ...(props["aria-label"] || props.placeholder
        ? { "aria-label": props["aria-label"] || props.placeholder! }
        : {}),
      ...(props.placeholder ? { "aria-placeholder": props.placeholder } : {}),
      ...(props["aria-keyshortcuts"] ? { "aria-keyshortcuts": props["aria-keyshortcuts"] } : {}),
      ...(props["aria-describedby"] ? { "aria-describedby": props["aria-describedby"] } : {}),
      ...(props.role ? { role: props.role } : {}),
      ...(props["aria-autocomplete"] ? { "aria-autocomplete": props["aria-autocomplete"] } : {}),
      ...(props["aria-controls"] ? { "aria-controls": props["aria-controls"] } : {}),
      ...(props["aria-expanded"] !== undefined
        ? { "aria-expanded": String(props["aria-expanded"]) }
        : {}),
      ...(props["aria-activedescendant"]
        ? { "aria-activedescendant": props["aria-activedescendant"] }
        : {}),
      ...(props.id ? { id: props.id } : {}),
      ...(props.inputId ? { [environment.inputIdAttribute]: props.inputId } : {}),
      ...(props["data-testid"] ? { "data-testid": props["data-testid"] } : {}),
      ...(props["data-has-draft"] ? { "data-has-draft": props["data-has-draft"] } : {}),
      ...(props["data-slash-command"] ? { "data-slash-command": props["data-slash-command"] } : {}),
    }),
  ];
}

/** Line breaks as the document stores them, so a value measures the same as its document. */
function normalizeLineBreaks(text: string): string {
  return text.replace(/\r\n?/g, "\n");
}

/** The smallest change turning `current` into `next`, so undo history and mapped positions stay stable. */
function minimalChange(current: string, next: string) {
  let prefix = 0;
  const limit = Math.min(current.length, next.length);
  while (prefix < limit && current[prefix] === next[prefix]) prefix += 1;
  let suffix = 0;
  while (
    suffix < limit - prefix &&
    current[current.length - 1 - suffix] === next[next.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  return {
    from: prefix,
    to: current.length - suffix,
    insert: next.slice(prefix, next.length - suffix),
  };
}

/** The surface shared composer logic edits, over one live EditorView. */
function createSurface(view: EditorView): ComposerEditorSurface {
  const clamp = (offset: number) => Math.max(0, Math.min(offset, view.state.doc.length));
  return {
    element: view.contentDOM,
    get isConnected() {
      return view.dom.isConnected;
    },
    get value() {
      return view.state.doc.toString();
    },
    get selectionStart() {
      return view.state.selection.main.from;
    },
    get selectionEnd() {
      return view.state.selection.main.to;
    },
    get scrollTop() {
      return view.scrollDOM.scrollTop;
    },
    set scrollTop(value) {
      view.scrollDOM.scrollTop = value;
    },
    get scrollLeft() {
      return view.scrollDOM.scrollLeft;
    },
    set scrollLeft(value) {
      view.scrollDOM.scrollLeft = value;
    },
    setSelectionRange(start, end) {
      // As a textarea does: a start past the end collapses to the end.
      const to = clamp(end);
      view.dispatch({ selection: EditorSelection.single(Math.min(clamp(start), to), to) });
    },
    focus() {
      // Not contentDOM.focus(): the browser would put its caret at the start and the editor
      // would take that as the user's selection. view.focus() keeps the editor's selection.
      view.focus();
    },
    blur() {
      view.contentDOM.blur();
    },
    insertText(text, start, end) {
      // Shared composer edits (paste, ⌥↵, list continuation) never change a locked draft.
      if (view.state.readOnly) return false;
      const from = clamp(start);
      const insert = normalizeLineBreaks(text);
      view.dispatch({
        changes: { from, to: clamp(end), insert },
        selection: { anchor: from + insert.length },
        userEvent: "input",
        // Each programmatic insert is its own undo step, not joined to the typing around it.
        annotations: isolateHistory.of("full"),
      });
      return true;
    },
    revealCaret() {
      view.dispatch({ effects: EditorView.scrollIntoView(view.state.selection.main.head) });
    },
  };
}

/** The CodeMirror composer input for `environment`; the loader makes one per page. */
export function createCodeMirrorComposerInput(environment: CodeMirrorComposerEnvironment) {
  return forwardRef<ComposerTextTarget, Omit<ComposerInputProps, "ref">>(
    function CodeMirrorComposerInput(props, forwardedRef) {
      return (
        <CodeMirrorComposer {...props} environment={environment} forwardedRef={forwardedRef} />
      );
    },
  );
}

/** Bring a controlled value in without echoing a change or adding an undo step. */
function syncControlledValue(view: EditorView, value: string) {
  const next = normalizeLineBreaks(value);
  const current = view.state.doc.toString();
  if (current === next) return;
  view.dispatch({
    changes: minimalChange(current, next),
    // As a textarea's value setter does: a new value puts the caret at its end.
    selection: EditorSelection.cursor(next.length),
    annotations: [controlledUpdate.of(true), Transaction.addToHistory.of(false)],
  });
}

function CodeMirrorComposer({
  environment,
  forwardedRef,
  ...props
}: Omit<ComposerInputProps, "ref"> & {
  environment: CodeMirrorComposerEnvironment;
  forwardedRef: Ref<ComposerTextTarget>;
}) {
  const { value, placeholder, autoFocus, heightMode = "compact", className } = props;
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const surfaceRef = useRef<ComposerEditorSurface | null>(null);
  const latest = useRef(props);
  useLayoutEffect(() => {
    latest.current = props;
  });
  const compartments = useMemo(
    () => ({
      attributes: new Compartment(),
      placeholder: new Compartment(),
      theme: new Compartment(),
    }),
    [],
  );

  // The editor is created once; the effects below reconfigure it.
  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const event = (): ComposerInputEvent => ({ currentTarget: surfaceRef.current! });
    const initial = latest.current;
    const view = new EditorView({
      parent: host,
      state: EditorState.create({
        doc: initial.value ?? initial.defaultValue ?? "",
        extensions: [
          EditorState.allowMultipleSelections.of(false),
          // Composer keys run first; CodeMirror's own bindings only see keys they pass on.
          Prec.highest(
            EditorView.domEventHandlers({
              keydown(keyEvent, editor) {
                if (keyEvent.isComposing || editor.composing || keyEvent.keyCode === 229) {
                  return false;
                }
                const composerEvent: ComposerKeyboardEvent = {
                  key: keyEvent.key,
                  code: keyEvent.code,
                  altKey: keyEvent.altKey,
                  ctrlKey: keyEvent.ctrlKey,
                  metaKey: keyEvent.metaKey,
                  shiftKey: keyEvent.shiftKey,
                  repeat: keyEvent.repeat,
                  get defaultPrevented() {
                    return keyEvent.defaultPrevented;
                  },
                  nativeEvent: keyEvent,
                  currentTarget: surfaceRef.current!,
                  preventDefault: () => keyEvent.preventDefault(),
                  stopPropagation: () => keyEvent.stopPropagation(),
                };
                latest.current.onKeyDown?.(composerEvent);
                return keyEvent.defaultPrevented;
              },
              paste(pasteEvent) {
                if (!pasteEvent.clipboardData) return false;
                latest.current.onPaste?.({
                  clipboardData: pasteEvent.clipboardData,
                  currentTarget: surfaceRef.current!,
                  preventDefault: () => pasteEvent.preventDefault(),
                });
                return pasteEvent.defaultPrevented;
              },
              focus() {
                latest.current.onFocus?.(event());
                return false;
              },
              blur() {
                latest.current.onBlur?.(event());
                return false;
              },
              compositionstart() {
                latest.current.onCompositionStart?.();
                return false;
              },
              compositionend() {
                latest.current.onCompositionEnd?.();
                return false;
              },
            }),
          ),
          keymap.of(COMPOSER_EDITING_KEYMAP),
          history(),
          EditorView.lineWrapping,
          compartments.attributes.of(attributes(initial, environment)),
          compartments.placeholder.of(placeholderText(initial.placeholder ?? "")),
          compartments.theme.of(theme(initial.heightMode ?? "compact")),
          EditorView.updateListener.of((update) => {
            const fromUser = !update.transactions.some((transaction) =>
              transaction.annotation(controlledUpdate),
            );
            if (update.docChanged && fromUser) {
              const fromHistory = update.transactions.some(
                (transaction) => transaction.isUserEvent("undo") || transaction.isUserEvent("redo"),
              );
              latest.current.onChange?.({ ...event(), fromHistory });
              // A parent that rejects the edit may not re-render. Reconcile once the parent's
              // update, if any, has committed, as a textarea does after an event.
              queueMicrotask(() => {
                const parentValue = latest.current.value;
                if (parentValue !== undefined && viewRef.current)
                  syncControlledValue(viewRef.current, parentValue);
              });
            }
            if (update.docChanged || update.selectionSet) latest.current.onSelect?.(event());
            if (update.docChanged || update.geometryChanged) markOverflow(update.view);
          }),
        ],
      }),
    });
    const markOverflow = (editor: EditorView) => {
      const scroller = editor.scrollDOM;
      editor.dom.toggleAttribute("data-overflowing", scroller.scrollHeight > scroller.clientHeight);
      editor.dom.toggleAttribute("data-scrolled", scroller.scrollTop > 0);
    };
    const onScroll = () => {
      markOverflow(view);
      latest.current.onScroll?.(event());
    };
    view.scrollDOM.addEventListener("scroll", onScroll, { passive: true });
    const surface = createSurface(view);
    viewRef.current = view;
    surfaceRef.current = surface;
    const unregister = environment.registerSurface(surface);
    return () => {
      unregister();
      view.scrollDOM.removeEventListener("scroll", onScroll);
      view.destroy();
      viewRef.current = null;
      surfaceRef.current = null;
    };
  }, [compartments, environment]);
  // React re-attaches the surface when the forwarded ref changes, as for a textarea.
  useImperativeHandle(forwardedRef, () => surfaceRef.current!, []);

  // Runs on every render, so an edit the parent rejected reverts even when the value didn't change.
  useLayoutEffect(() => {
    const view = viewRef.current;
    if (view && value !== undefined) syncControlledValue(view, value);
  });

  // One key for every prop that becomes an editor or content attribute.
  const attributeKey = JSON.stringify([
    props.placeholder,
    props.disabled,
    props.readOnly,
    props["aria-label"],
    props["aria-keyshortcuts"],
    props["aria-describedby"],
    props.role,
    props["aria-autocomplete"],
    props["aria-controls"],
    props["aria-expanded"],
    props["aria-activedescendant"],
    props.id,
    props.inputId,
    props["data-testid"],
    props["data-has-draft"],
    props["data-slash-command"],
  ]);
  // In layout, so a draft that turns disabled or read-only takes no keys after the render.
  useLayoutEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: compartments.attributes.reconfigure(attributes(latest.current, environment)),
    });
    // CodeMirror always marks its content aria-multiline, which a combobox doesn't take. It only
    // rewrites attributes whose value changed, so removing it here holds until the role changes.
    if (latest.current.role === "combobox") view.contentDOM.removeAttribute("aria-multiline");
    else view.contentDOM.setAttribute("aria-multiline", "true");
  }, [compartments, attributeKey, environment]);
  useEffect(() => {
    viewRef.current?.dispatch({
      effects: compartments.placeholder.reconfigure(placeholderText(placeholder ?? "")),
    });
  }, [compartments, placeholder]);
  useEffect(() => {
    viewRef.current?.dispatch({ effects: compartments.theme.reconfigure(theme(heightMode)) });
  }, [compartments, heightMode]);
  useEffect(() => {
    if (autoFocus) viewRef.current?.focus();
  }, [autoFocus]);

  return (
    <div
      ref={hostRef}
      data-composer-engine="codemirror"
      // A caller's text size replaces the default one, as the floating panel's does.
      className={cn(
        "composer-input-text relative w-full text-ui text-foreground md:min-h-[42px]",
        props.disabled && "opacity-60",
        className,
      )}
    />
  );
}
