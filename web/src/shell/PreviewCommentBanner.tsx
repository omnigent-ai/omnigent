import { MessageSquarePlusIcon } from "lucide-react";
import { useCanComment } from "@/hooks/usePermissions";

/**
 * Shown atop a rendered Markdown preview that can't map selections back to the
 * source (rewritten math delimiters, very large files), pointing users at the
 * editor for commenting. Other previews take comments directly.
 *
 * Only rendered for users who can comment (project files are read-only but
 * take comments); anyone else has no comment action to reach.
 */
export function PreviewCommentBanner({
  conversationId,
  onSwitchToEdit,
}: {
  conversationId: string;
  onSwitchToEdit: () => void;
}) {
  const canComment = useCanComment(conversationId);
  if (!canComment) return null;
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
