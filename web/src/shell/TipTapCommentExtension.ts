// ProseMirror Plugin + TipTap Extension for comment decorations.
//
// Decorations are used (not marks) so highlights never affect markdown serialisation
// and remap automatically through editing transactions.
//
// stateRef is closed over directly in createCommentDecorationExtension() rather
// than passed through configure()/addOptions(): TipTap deep-merges options, which
// clones the ref object and breaks the shared-ref contract.

import { Extension } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import type { RefObject } from "react";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";
import { findPmRangeForComment } from "./TipTapEditorHelpers";

export const commentDecorationKey = new PluginKey<DecorationSet>("commentDecoration");

export interface CommentDecorationState {
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  rawContent: string;
  /** PM range for the in-progress "pending" comment (blue highlight). */
  pendingRange: { from: number; to: number } | null;
  /** Called when the user clicks a comment decoration in the editor. */
  onClickComment: (comment: Comment) => void;
}

function buildDecorations(doc: ProseMirrorNode, state: CommentDecorationState): DecorationSet {
  const { comments, activeSelection, rawContent, pendingRange } = state;
  const decos: Decoration[] = [];

  for (const comment of comments) {
    const range = findPmRangeForComment(doc, comment, rawContent);
    if (!range) continue;
    const isActive =
      activeSelection?.start_index === comment.start_index &&
      activeSelection?.end_index === comment.end_index;
    decos.push(
      Decoration.inline(range.from, range.to, {
        class: isActive ? "md-comment md-comment-active" : "md-comment",
        "data-comment-id": comment.id,
      }),
    );
  }

  if (pendingRange) {
    decos.push(
      Decoration.inline(pendingRange.from, pendingRange.to, {
        class: "md-comment-pending",
      }),
    );
  }

  return DecorationSet.create(doc, decos);
}

/**
 * Creates a TipTap Extension that overlays comment highlights as ProseMirror Decorations.
 *
 * After mutating stateRef.current, trigger a redraw by dispatching:
 *   editor.state.tr.setMeta(commentDecorationKey, 'rebuild')
 */
export function createCommentDecorationExtension(
  stateRef: RefObject<CommentDecorationState | null>,
) {
  return Extension.create({
    name: "commentDecoration",

    addProseMirrorPlugins() {
      return [
        new Plugin({
          key: commentDecorationKey,
          state: {
            init(_, { doc }) {
              return stateRef.current
                ? buildDecorations(doc, stateRef.current)
                : DecorationSet.empty;
            },
            apply(tr, decorations, _, newState) {
              const meta = tr.getMeta(commentDecorationKey) as
                | "rebuild"
                | { pendingRange: { from: number; to: number } | null }
                | undefined;
              if (meta) {
                if (!stateRef.current) return DecorationSet.empty;
                // Explicit pendingRange in meta is written to stateRef here so
                // the decoration appears before the React sync-effect fires.
                if (typeof meta === "object" && "pendingRange" in meta) {
                  stateRef.current.pendingRange = meta.pendingRange;
                }
                return buildDecorations(newState.doc, stateRef.current);
              }
              // Remap existing decorations through the transaction automatically.
              return decorations.map(tr.mapping, newState.doc);
            },
          },
          props: {
            decorations(state) {
              return this.getState(state) ?? DecorationSet.empty;
            },
            handleDOMEvents: {
              click(_, event) {
                const target = (event.target as Element).closest("[data-comment-id]");
                if (!target) return false;
                const id = target.getAttribute("data-comment-id");
                if (!id || !stateRef.current) return false;
                const comment = stateRef.current.comments.find((c) => c.id === id);
                if (comment) {
                  stateRef.current.onClickComment(comment);
                  return true;
                }
                return false;
              },
            },
          },
        }),
      ];
    },
  });
}
