import { Badge } from "@/components/ui/badge";
import { NeedsResponseBadge } from "@/components/NeedsResponseBadge";
import type { TicketTag } from "@/lib/projectTicketsApi";

export { TicketStageIcon } from "./TicketStageAppearance";

export function TicketTags({ tags }: { tags: TicketTag[] }) {
  return (
    <div className="flex min-w-0 flex-wrap gap-1.5">
      {tags.map((tag) =>
        tag.key === "needs-human" ? (
          <NeedsResponseBadge key={tag.key} title={tag.value || undefined} />
        ) : (
          <Badge
            key={tag.key}
            variant="secondary"
            title={tag.value || undefined}
            className="h-auto min-h-5 max-w-full whitespace-normal text-left wrap-anywhere"
          >
            {`${tag.key}${tag.value ? `: ${tag.value}` : ""}`}
          </Badge>
        ),
      )}
    </div>
  );
}

export function TicketError({ error }: { error: Error | null }) {
  return error ? (
    <p role="alert" className="text-sm text-destructive">
      {error.message}
    </p>
  ) : null;
}
