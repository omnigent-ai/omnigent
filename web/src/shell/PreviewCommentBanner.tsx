import { MessageSquarePlusIcon } from "lucide-react";
import { useCanEdit } from "@/hooks/usePermissions";

/**
 * Shown atop the rendered Markdown preview to point users at the editor for
 * commenting. The rendered preview has no source-offset mapping, so text-
 * selection comments only work on the rich-text Editor surface (and Source);
 * rather than a second, fuzzier comment path here, we nudge the user to switch.
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
