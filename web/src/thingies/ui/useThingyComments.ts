// Comments on a thingy, in the shape of the comments panel that files use. They live with the
// thingy, so everyone it is shared with sees them; Address all sends the open ones to the session
// that works on the thingy and moves them to Sent.
import { useCallback, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection, CommentsPanelProps } from "@/shell/CommentsPanel";
import {
  type Thingy,
  type ThingyComment,
  type ThingyCommentAnchor,
  createThingyComment,
  deleteThingyComment,
  listThingyComments,
  updateThingyComment,
} from "@/thingies/api/thingyApi";
import type { ThingySelection } from "@/thingies/host/thingyBridge";
import { THINGIES_KEY } from "./thingyQueries";

export function thingyCommentsKey(thingyId: string) {
  return [...THINGIES_KEY, "comments", thingyId] as const;
}

/** What the session reads for the comments Address all sends. */
export function thingyCommentsMessage(
  thingy: Pick<Thingy, "id" | "name">,
  comments: readonly ThingyComment[],
): string {
  const lines = [
    `Address these comments on the thingy ${JSON.stringify(thingy.name)} (thingy_id ${thingy.id}). Change its page with thingy_revision_create and thingy_publish, or its records, as each asks. They are already marked addressed; thingy_comment_list has them all.`,
    "",
  ];
  comments.forEach((comment, index) => {
    const where = [comment.anchor.label, comment.anchor.ref ? `ref ${comment.anchor.ref}` : null]
      .filter(Boolean)
      .join(", ");
    lines.push(
      `${index + 1}. On ${JSON.stringify(comment.anchor.text)}${where ? ` (${where})` : ""}, ${comment.createdBy || "someone"} wrote:`,
      ...comment.body.split("\n").map((line) => `   ${line}`),
    );
  });
  return lines.join("\n");
}

// The panel tells a comment's card from a pending selection by character range. Thingy comments
// have none, so each gets its own position and a pending selection one no comment has.
const PENDING_POSITION = -1;

function toPanelComment(thingyId: string, comment: ThingyComment, position: number): Comment {
  return {
    id: comment.id,
    conversation_id: "",
    path: `thingy:${thingyId}`,
    start_index: position,
    end_index: position,
    body: comment.body,
    status: comment.status,
    created_at: comment.createdAt,
    updated_at: comment.updatedAt,
    anchor_content: comment.anchor.text,
    created_by: comment.createdBy || null,
  };
}

export type ThingyCommentsPanelProps = Pick<
  CommentsPanelProps,
  | "comments"
  | "addressedComments"
  | "activeSelection"
  | "onAddComment"
  | "onAddressAll"
  | "onEditComment"
  | "onDeleteComment"
  | "onClickComment"
  | "canAddress"
  | "addressPending"
  | "canEdit"
>;

export interface ThingyComments {
  open: boolean;
  setOpen: (open: boolean) => void;
  draftCount: number;
  canComment: boolean;
  /** Starts a comment on a selection in the thingy and opens the panel. */
  startComment: (selection: ThingySelection) => void;
  panel: ThingyCommentsPanelProps;
  error: unknown;
}

export function useThingyComments(
  thingy: Thingy,
  /** Sends the message to the session working on the thingy; absent when the viewer can't. */
  address: ((message: string) => Promise<void>) | undefined,
): ThingyComments {
  const thingyId = thingy.id;
  const queryClient = useQueryClient();
  const list = useQuery({
    queryKey: thingyCommentsKey(thingyId),
    queryFn: ({ signal }) => listThingyComments(thingyId, signal),
  });
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<ThingyCommentAnchor | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [addressing, setAddressing] = useState(false);
  const [workError, setWorkError] = useState<unknown>(undefined);

  const run = useCallback(
    async (work: () => Promise<unknown>) => {
      try {
        await work();
        setWorkError(undefined);
      } catch (error) {
        setWorkError(error);
      }
      await queryClient.invalidateQueries({ queryKey: thingyCommentsKey(thingyId) });
    },
    [queryClient, thingyId],
  );

  const items = useMemo(() => list.data?.items ?? [], [list.data]);
  const canComment = list.data?.canComment ?? false;
  const drafts = useMemo(() => items.filter((item) => item.status === "draft"), [items]);
  const positions = useMemo(
    () => new Map(items.map((item, index) => [item.id, index + 1])),
    [items],
  );
  const toPanel = (item: ThingyComment) =>
    toPanelComment(thingyId, item, positions.get(item.id) ?? 0);

  const startComment = useCallback((selection: ThingySelection) => {
    setPending({
      text: selection.text,
      label: selection.label ?? null,
      ref: selection.ref ?? null,
      occurrence: selection.occurrence,
    });
    setSelectedId(null);
    setOpen(true);
  }, []);

  const selected = items.find((item) => item.id === selectedId);
  const activeSelection: ActiveSelection | null = useMemo(
    () =>
      pending
        ? {
            start_index: PENDING_POSITION,
            end_index: PENDING_POSITION,
            anchor_content: pending.text,
          }
        : selected
          ? {
              start_index: positions.get(selected.id) ?? 0,
              end_index: positions.get(selected.id) ?? 0,
              anchor_content: selected.anchor.text,
              comment_id: selected.id,
            }
          : null,
    [pending, positions, selected],
  );

  return {
    open,
    setOpen,
    draftCount: drafts.length,
    canComment,
    startComment,
    error: workError ?? (list.isError ? list.error : undefined),
    panel: {
      comments: drafts.map(toPanel),
      addressedComments: items.filter((item) => item.status === "addressed").map(toPanel),
      activeSelection,
      onAddComment: (body) => {
        const anchor = pending;
        if (anchor === null) return;
        setPending(null);
        void run(() =>
          createThingyComment(thingyId, { body, anchor, operationId: crypto.randomUUID() }),
        );
      },
      onAddressAll: () => {
        if (address === undefined || drafts.length === 0) return;
        setAddressing(true);
        void run(async () => {
          await address(thingyCommentsMessage(thingy, drafts));
          await Promise.all(
            drafts.map((item) => updateThingyComment(thingyId, item.id, { status: "addressed" })),
          );
        }).finally(() => setAddressing(false));
      },
      onEditComment: (id, body) => void run(() => updateThingyComment(thingyId, id, { body })),
      onDeleteComment: (id) => void run(() => deleteThingyComment(thingyId, id)),
      onClickComment: (comment) => {
        setPending(null);
        setSelectedId(comment.id);
      },
      canAddress: address !== undefined,
      addressPending: addressing,
      canEdit: canComment,
    },
  };
}
