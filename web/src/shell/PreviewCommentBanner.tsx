import { MessageSquarePlusIcon } from "lucide-react";
import { useCanEdit } from "@/hooks/usePermissions";

/**
 * Shown atop the preview of large Markdown to point users at the editor for
 * commenting. That preview skips the source mapping that makes smaller
 * previews commentable, so selections there can only be commented on in Source.
 *
 * Only rendered for users who can edit — a read-only viewer has no comment
 * action to reach, so the hint would be noise.
 */
export function PreviewCommentBanner({
  conversationId,
  onSwitchToEdit,
}: {
  conversationId: string;
  onSwitchToEdit: () => void;
}) {
  const canEdit = useCanEdit(conversationId);
  if (!canEdit) return null;
  return (
    <div className="flex items-center gap-2 border-b border-border bg-muted/40 px-4 py-1.5 text-sm text-muted-foreground shrink-0">
      <MessageSquarePlusIcon className="size-3.5 shrink-0" />
      <span>
        To comment on this file,{" "}
        <button
          type="button"
          onClick={onSwitchToEdit}
          className="cursor-pointer font-medium text-foreground underline underline-offset-2 hover:text-primary"
        >
          switch to edit mode
        </button>
        .
      </span>
    </div>
  );
}
