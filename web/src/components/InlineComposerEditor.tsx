import { Extension, Node as TiptapNode, type JSONContent } from "@tiptap/core";
import { Fragment, Slice, type Node as ProseMirrorNode } from "@tiptap/pm/model";
import { NodeSelection, Plugin, PluginKey, TextSelection } from "@tiptap/pm/state";
import { Decoration, DecorationSet, type EditorView } from "@tiptap/pm/view";
import { EditorContent, useEditor } from "@tiptap/react";
import TiptapStarterKit from "@tiptap/starter-kit";
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, type RefObject } from "react";

import {
  COMPOSER_ATTACHMENT_PLACEHOLDER,
  composerPartsFromProjection,
  composerPartsToProjection,
  composerPartsToText,
  normalizeComposerParts,
  replaceComposerText,
  type ComposerDraftPart,
} from "@/lib/composerContent";
import { validateAttachments } from "@/lib/attachments";

const ATTACHMENT_NODE = "composerAttachment";

const SlashTokenHighlight = Extension.create({
  name: "slashTokenHighlight",
  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: new PluginKey("slashTokenHighlight"),
        props: {
          decorations(state) {
            const firstBlock = state.doc.firstChild;
            if (!firstBlock) return null;
            const match = /^(\s*)(\/[A-Za-z][\w-]*)(?=\s|$)/.exec(firstBlock.textContent);
            if (!match) return null;
            const from = 1 + match[1].length;
            return DecorationSet.create(state.doc, [
              Decoration.inline(from, from + match[2].length, {
                class: "text-brand-accent",
                "data-composer-slash-token": "true",
              }),
            ]);
          },
        },
      }),
    ];
  },
});

function appendText(parts: ComposerDraftPart[], text: string): void {
  if (!text) return;
  const tail = parts.at(-1);
  if (tail?.type === "text") tail.text += text;
  else parts.push({ type: "text", text });
}

function partsToDocument(
  parts: readonly ComposerDraftPart[],
  register: (file: File) => string,
): JSONContent {
  const paragraphs: JSONContent[] = [];
  let inline: JSONContent[] = [];
  const finishParagraph = () => {
    paragraphs.push({ type: "paragraph", ...(inline.length > 0 ? { content: inline } : {}) });
    inline = [];
  };

  for (const part of normalizeComposerParts(parts)) {
    if (part.type === "attachment") {
      inline.push({
        type: ATTACHMENT_NODE,
        attrs: {
          attachmentId: register(part.file),
          name: part.file.name || "image.png",
          mime: part.file.type || "application/octet-stream",
        },
      });
      continue;
    }
    const lines = part.text.split("\n");
    lines.forEach((line, index) => {
      if (line) inline.push({ type: "text", text: line });
      if (index < lines.length - 1) finishParagraph();
    });
  }
  finishParagraph();
  return { type: "doc", content: paragraphs };
}

function documentToParts(
  doc: ProseMirrorNode,
  files: ReadonlyMap<string, File>,
): ComposerDraftPart[] {
  const parts: ComposerDraftPart[] = [];
  doc.forEach((block, _offset, blockIndex) => {
    if (blockIndex > 0) appendText(parts, "\n");
    block.forEach((node) => {
      if (node.isText) appendText(parts, node.text ?? "");
      else if (node.type.name === "hardBreak") appendText(parts, "\n");
      else if (node.type.name === ATTACHMENT_NODE) {
        const file = files.get(String(node.attrs.attachmentId));
        if (file) parts.push({ type: "attachment", file });
      }
    });
  });
  return normalizeComposerParts(parts);
}

function projectionOffsetToPosition(doc: ProseMirrorNode, rawOffset: number): number {
  const projectionLength = documentProjectionLength(doc);
  const offset = Math.max(0, Math.min(rawOffset, projectionLength));
  let consumed = 0;
  let fallback = 1;
  let found: number | null = null;

  doc.forEach((block, blockOffset, blockIndex) => {
    if (found !== null) return;
    if (blockIndex > 0) {
      if (offset === consumed) {
        found = blockOffset + 1;
        return;
      }
      consumed += 1;
    }
    const length = block.content.size;
    if (offset <= consumed + length) {
      found = blockOffset + 1 + (offset - consumed);
      return;
    }
    consumed += length;
    fallback = blockOffset + 1 + length;
  });
  return found ?? fallback;
}

function documentProjectionLength(doc: ProseMirrorNode): number {
  let length = 0;
  doc.forEach((block, _offset, index) => {
    if (index > 0) length += 1;
    length += block.content.size;
  });
  return length;
}

function positionToProjectionOffset(doc: ProseMirrorNode, rawPosition: number): number {
  let projection = 0;
  let result = 0;
  let found = false;
  doc.forEach((block, blockOffset, blockIndex) => {
    if (found) return;
    if (blockIndex > 0) projection += 1;
    const start = blockOffset + 1;
    const end = start + block.content.size;
    if (rawPosition <= end) {
      result = projection + Math.max(0, rawPosition - start);
      found = true;
      return;
    }
    projection += block.content.size;
    result = projection;
  });
  return result;
}

function createAttachmentExtension(filesRef: RefObject<Map<string, File>>) {
  return TiptapNode.create({
    name: ATTACHMENT_NODE,
    group: "inline",
    inline: true,
    atom: true,
    selectable: true,
    draggable: true,
    addAttributes() {
      return {
        attachmentId: { default: "" },
        name: { default: "file" },
        mime: { default: "application/octet-stream" },
      };
    },
    parseHTML() {
      return [{ tag: "span[data-composer-attachment]" }];
    },
    renderHTML({ HTMLAttributes }) {
      return ["span", { ...HTMLAttributes, "data-composer-attachment": "true" }];
    },
    addKeyboardShortcuts() {
      const move = (direction: -1 | 1): boolean => {
        const { state, view } = this.editor;
        const { selection } = state;
        if (!(selection instanceof NodeSelection) || selection.node.type.name !== ATTACHMENT_NODE) {
          return false;
        }
        const node = selection.node;
        const from = selection.from;
        const projectionOffset = positionToProjectionOffset(state.doc, from);
        const transaction = state.tr.delete(selection.from, selection.to);
        const target = projectionOffsetToPosition(transaction.doc, projectionOffset + direction);
        if (target === from && direction < 0 && projectionOffset === 0) return true;
        transaction.insert(target, node);
        transaction.setSelection(NodeSelection.create(transaction.doc, target));
        view.dispatch(transaction.scrollIntoView());
        return true;
      };
      return {
        "Alt-ArrowLeft": () => move(-1),
        "Alt-ArrowRight": () => move(1),
      };
    },
    addNodeView() {
      return ({ node, editor, getPos }) => {
        const dom = document.createElement("span");
        dom.dataset.composerAttachment = "true";
        dom.contentEditable = "false";
        dom.draggable = true;
        dom.className =
          "inline-flex min-h-7 max-w-[220px] cursor-grab select-none items-center gap-1.5 rounded-lg border border-border bg-muted px-1 py-0.5 align-middle text-sm text-foreground active:cursor-grabbing";
        dom.setAttribute(
          "aria-label",
          `${String(node.attrs.mime).startsWith("image/") ? "Image" : "File"} attachment ${node.attrs.name}. Drag to move; Alt plus arrow keys also move it.`,
        );
        dom.setAttribute("role", "group");

        const preview = document.createElement("span");
        preview.className =
          "grid size-6 shrink-0 place-items-center overflow-hidden rounded-md bg-background text-xs text-muted-foreground";
        const file = filesRef.current?.get(String(node.attrs.attachmentId));
        let objectUrl: string | null = null;
        if (file && file.type.startsWith("image/") && typeof URL.createObjectURL === "function") {
          objectUrl = URL.createObjectURL(file);
          const image = document.createElement("img");
          image.src = objectUrl;
          image.alt = "";
          image.className = "size-full object-cover";
          preview.append(image);
        } else {
          preview.textContent = String(node.attrs.mime).startsWith("image/") ? "▧" : "F";
        }

        const label = document.createElement("span");
        label.className = "truncate";
        label.textContent = String(node.attrs.name);

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className =
          "grid size-6 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-background hover:text-foreground";
        remove.setAttribute("aria-label", `Remove ${node.attrs.name}`);
        remove.textContent = "×";
        remove.addEventListener("mousedown", (event) => event.preventDefault());
        remove.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          const position = typeof getPos === "function" ? getPos() : undefined;
          if (typeof position !== "number") return;
          editor
            .chain()
            .focus()
            .deleteRange({ from: position, to: position + node.nodeSize })
            .run();
        });
        dom.append(preview, label, remove);

        return {
          dom,
          destroy() {
            if (objectUrl) URL.revokeObjectURL(objectUrl);
          },
        };
      };
    },
  });
}

export interface InlineComposerEditorHandle {
  focus: () => void;
  getParts: () => ComposerDraftPart[];
  getProjection: () => string;
  getSelection: () => { start: number; end: number };
  setSelection: (offset: number, endOffset?: number) => void;
  setParts: (parts: readonly ComposerDraftPart[]) => void;
  insertFiles: (files: readonly File[]) => void;
  selectAttachment: (index: number) => void;
}

interface InlineComposerEditorProps {
  initialParts: readonly ComposerDraftPart[];
  onChange: (parts: ComposerDraftPart[]) => void;
  onRejectedFiles?: (messages: string[]) => void;
  onFocus?: () => void;
  onBlur?: () => void;
  onCompositionStart?: () => void;
  onCompositionEnd?: () => void;
  onKeyDown?: (event: KeyboardEvent, selection: { start: number; end: number }) => boolean;
  ariaLabel: string;
  testId?: string;
  autoFocus?: boolean;
  disabled?: boolean;
  placeholder?: string;
  className?: string;
  editorClassName?: string;
}

export const InlineComposerEditor = forwardRef<
  InlineComposerEditorHandle,
  InlineComposerEditorProps
>(function InlineComposerEditor(
  {
    initialParts,
    onChange,
    onRejectedFiles,
    onFocus,
    onBlur,
    onCompositionStart,
    onCompositionEnd,
    onKeyDown,
    ariaLabel,
    testId,
    autoFocus = false,
    disabled = false,
    placeholder = "",
    className = "",
    editorClassName = "min-h-[24px] max-h-[208px]",
  },
  ref,
) {
  const filesRef = useRef(new Map<string, File>());
  const nextIdRef = useRef(0);
  const onChangeRef = useRef(onChange);
  const onRejectedFilesRef = useRef(onRejectedFiles);
  const onFocusRef = useRef(onFocus);
  const onBlurRef = useRef(onBlur);
  const onKeyDownRef = useRef(onKeyDown);
  onChangeRef.current = onChange;
  onRejectedFilesRef.current = onRejectedFiles;
  onFocusRef.current = onFocus;
  onBlurRef.current = onBlur;
  onKeyDownRef.current = onKeyDown;

  const register = (file: File): string => {
    const id = `composer-file-${nextIdRef.current++}`;
    filesRef.current.set(id, file);
    return id;
  };
  const registerRef = useRef(register);
  registerRef.current = register;
  const initialDocumentRef = useRef<JSONContent | null>(null);
  if (initialDocumentRef.current === null) {
    initialDocumentRef.current = partsToDocument(initialParts, register);
  }

  const extensions = useMemo(
    () => [
      TiptapStarterKit.configure({
        blockquote: false,
        bulletList: false,
        code: false,
        codeBlock: false,
        heading: false,
        horizontalRule: false,
        orderedList: false,
        dropcursor: { color: "var(--ring)", width: 2 },
      }),
      createAttachmentExtension(filesRef),
      SlashTokenHighlight,
    ],
    [],
  );

  const insertFilesIntoView = (view: EditorView, incoming: readonly File[]) => {
    const { accepted, errors } = validateAttachments([...incoming]);
    onRejectedFilesRef.current?.(errors);
    if (accepted.length === 0) return;
    const nodes = accepted.map((file) =>
      view.state.schema.nodes[ATTACHMENT_NODE].create({
        attachmentId: registerRef.current(file),
        name: file.name || "image.png",
        mime: file.type || "application/octet-stream",
      }),
    );
    const transaction = view.state.tr.replaceSelection(new Slice(Fragment.fromArray(nodes), 0, 0));
    view.dispatch(transaction.scrollIntoView());
    view.focus();
  };

  const editor = useEditor({
    extensions,
    content: initialDocumentRef.current,
    editable: !disabled,
    editorProps: {
      attributes: {
        role: "textbox",
        "aria-multiline": "true",
        "aria-label": ariaLabel,
        ...(testId ? { "data-testid": testId } : {}),
        "data-placeholder": placeholder,
        class: `block ${editorClassName} overflow-y-auto whitespace-pre-wrap break-words px-4 pt-3 pb-2 outline-none [scrollbar-width:none] [&::-webkit-scrollbar]:hidden`,
      },
      handlePaste: (view, event) => {
        const pasted = Array.from(event.clipboardData?.files ?? []);
        if (pasted.length === 0) return false;
        event.preventDefault();
        insertFilesIntoView(view, pasted);
        return true;
      },
      handleDrop: (view, event, _slice, moved) => {
        if (moved) return false;
        const dropped = Array.from(event.dataTransfer?.files ?? []);
        if (dropped.length === 0) return false;
        event.preventDefault();
        const position = view.posAtCoords({ left: event.clientX, top: event.clientY })?.pos;
        if (position !== undefined) {
          view.dispatch(
            view.state.tr.setSelection(TextSelection.near(view.state.doc.resolve(position))),
          );
        }
        insertFilesIntoView(view, dropped);
        return true;
      },
      handleKeyDown: (view, event) => {
        const callback = onKeyDownRef.current;
        if (!callback) return false;
        return callback(event, {
          start: positionToProjectionOffset(view.state.doc, view.state.selection.from),
          end: positionToProjectionOffset(view.state.doc, view.state.selection.to),
        });
      },
    },
    onUpdate: ({ editor: updated }) => {
      onChangeRef.current(documentToParts(updated.state.doc, filesRef.current));
    },
    onFocus: () => onFocusRef.current?.(),
    onBlur: () => onBlurRef.current?.(),
  });

  useEffect(() => {
    editor?.setEditable(!disabled);
  }, [editor, disabled]);

  useEffect(() => {
    if (autoFocus) editor?.view.focus();
  }, [editor, autoFocus]);

  // Keep the small textarea-compatible DOM surface the surrounding composer
  // hooks use for caret-aware dictation, mentions, and existing integrations.
  // The visible editor remains ProseMirror; these properties simply translate
  // its document and selection to the same projection offsets.
  useEffect(() => {
    if (!editor) return;
    const dom = editor.view.dom as HTMLDivElement & {
      value: string;
      placeholder: string;
      disabled: boolean;
      selectionStart: number;
      selectionEnd: number;
      setSelectionRange: (start: number, end: number) => void;
    };
    Object.defineProperties(dom, {
      value: {
        configurable: true,
        get: () => composerPartsToText(documentToParts(editor.state.doc, filesRef.current)),
        set: (next: string) => {
          const currentParts = documentToParts(editor.state.doc, filesRef.current);
          const currentFiles = currentParts
            .filter((part) => part.type === "attachment")
            .map((part) => part.file);
          const nextValue = String(next);
          const nextParts =
            currentFiles.length > 0 && !nextValue.includes(COMPOSER_ATTACHMENT_PLACEHOLDER)
              ? replaceComposerText(currentParts, nextValue)
              : composerPartsFromProjection(nextValue, currentFiles);
          filesRef.current.clear();
          editor
            .chain()
            .setContent(partsToDocument(nextParts, registerRef.current), { emitUpdate: false })
            .command(({ tr }) => {
              tr.setMeta("addToHistory", false);
              return true;
            })
            .run();
          onChangeRef.current(documentToParts(editor.state.doc, filesRef.current));
        },
      },
      placeholder: { configurable: true, get: () => placeholder },
      disabled: { configurable: true, get: () => disabled },
      selectionStart: {
        configurable: true,
        get: () => positionToProjectionOffset(editor.state.doc, editor.state.selection.from),
        set: (offset: number) => {
          editor.commands.setTextSelection({
            from: projectionOffsetToPosition(editor.state.doc, offset),
            to: projectionOffsetToPosition(editor.state.doc, offset),
          });
        },
      },
      selectionEnd: {
        configurable: true,
        get: () => positionToProjectionOffset(editor.state.doc, editor.state.selection.to),
        set: (offset: number) => {
          const from = positionToProjectionOffset(editor.state.doc, editor.state.selection.from);
          editor.commands.setTextSelection({
            from: projectionOffsetToPosition(editor.state.doc, from),
            to: projectionOffsetToPosition(editor.state.doc, offset),
          });
        },
      },
      setSelectionRange: {
        configurable: true,
        value: (start: number, end: number) => {
          editor.commands.setTextSelection({
            from: projectionOffsetToPosition(editor.state.doc, start),
            to: projectionOffsetToPosition(editor.state.doc, end),
          });
        },
      },
    });
    const publishCompatibilityChange = () => {
      onChangeRef.current(documentToParts(editor.state.doc, filesRef.current));
    };
    dom.addEventListener("change", publishCompatibilityChange);
    return () => dom.removeEventListener("change", publishCompatibilityChange);
  }, [editor, placeholder, disabled]);

  useImperativeHandle(
    ref,
    () => ({
      focus: () => editor?.view.focus(),
      getParts: () => (editor ? documentToParts(editor.state.doc, filesRef.current) : []),
      getProjection: () =>
        editor
          ? composerPartsToProjection(documentToParts(editor.state.doc, filesRef.current))
          : "",
      getSelection: () =>
        editor
          ? {
              start: positionToProjectionOffset(editor.state.doc, editor.state.selection.from),
              end: positionToProjectionOffset(editor.state.doc, editor.state.selection.to),
            }
          : { start: 0, end: 0 },
      setSelection: (offset, endOffset = offset) => {
        if (!editor) return;
        editor.commands.setTextSelection({
          from: projectionOffsetToPosition(editor.state.doc, offset),
          to: projectionOffsetToPosition(editor.state.doc, endOffset),
        });
      },
      setParts: (parts) => {
        if (!editor) return;
        filesRef.current.clear();
        editor
          .chain()
          .setContent(partsToDocument(parts, registerRef.current), { emitUpdate: false })
          .command(({ tr }) => {
            tr.setMeta("addToHistory", false);
            return true;
          })
          .run();
        onChangeRef.current(documentToParts(editor.state.doc, filesRef.current));
      },
      insertFiles: (files) => {
        if (editor) insertFilesIntoView(editor.view, files);
      },
      selectAttachment: (index) => {
        if (!editor) return;
        let seen = 0;
        let position: number | null = null;
        editor.state.doc.descendants((node, pos) => {
          if (node.type.name !== ATTACHMENT_NODE) return;
          if (seen === index) position = pos;
          seen += 1;
        });
        if (position !== null) {
          editor.view.dispatch(
            editor.state.tr.setSelection(NodeSelection.create(editor.state.doc, position)),
          );
          editor.commands.focus();
        }
      },
    }),
    [editor],
  );

  return (
    <EditorContent
      editor={editor}
      onKeyDown={(event) => {
        if (event.defaultPrevented) return;
        const callback = onKeyDownRef.current;
        if (!callback || !editor) return;
        const selection = {
          start: positionToProjectionOffset(editor.state.doc, editor.state.selection.from),
          end: positionToProjectionOffset(editor.state.doc, editor.state.selection.to),
        };
        if (callback(event.nativeEvent, selection)) event.preventDefault();
      }}
      onCompositionStart={onCompositionStart}
      onCompositionEnd={onCompositionEnd}
      className={`${className} text-ui text-foreground [&_.ProseMirror]:relative [&_.ProseMirror_p]:m-0 [&_.ProseMirror:has(p:only-child:empty)::before]:pointer-events-none [&_.ProseMirror:has(p:only-child:empty)::before]:absolute [&_.ProseMirror:has(p:only-child:empty)::before]:text-muted-foreground [&_.ProseMirror:has(p:only-child:empty)::before]:content-[attr(data-placeholder)] [&_.ProseMirror-selectednode]:border-ring [&_.ProseMirror-selectednode]:ring-2 [&_.ProseMirror-selectednode]:ring-ring/20`}
    />
  );
});
