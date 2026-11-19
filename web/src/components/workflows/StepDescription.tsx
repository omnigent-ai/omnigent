import { ProseMarkdown } from "@/components/blocks/ChatMarkdown";
import { cn } from "@/lib/utils";

/**
 * A workflow step's whole description as Markdown: what it reads, does and
 * produces, then the details a run needs. Its parts sit closer together than a
 * chat message's, and their bold labels stand out from the muted text.
 */
export function StepDescription({ text, className }: { text: string; className?: string }) {
  return (
    <ProseMarkdown
      className={cn(
        "space-y-2 text-muted-foreground [&_[data-streamdown=strong]]:text-foreground",
        className,
      )}
    >
      {text}
    </ProseMarkdown>
  );
}
