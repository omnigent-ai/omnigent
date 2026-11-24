import { CheckIcon, LinkIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useCopyLink } from "@/hooks/useCopyLink";
import { useRebasePath } from "@/lib/routing";
import { getShareableTicketLink } from "@/lib/shareLink";

/**
 * The ticket header's Copy URL icon button. A check shows for 2 seconds after
 * copying; when the clipboard is unavailable a popover offers the URL to copy by hand.
 */
export function TicketCopyLinkButton({
  projectName,
  ticketId,
}: {
  projectName: string;
  ticketId: string;
}) {
  const rebasePath = useRebasePath();
  const { status, copy, reset } = useCopyLink();
  const url = getShareableTicketLink(projectName, ticketId, rebasePath);
  const label = status === "copied" ? "Copied" : "Copy URL";

  return (
    <Popover open={status === "failed"} onOpenChange={(open) => !open && reset()}>
      {/* A DOM anchor: React 18 function-component triggers drop the anchor's ref. */}
      <PopoverAnchor asChild>
        <span className="inline-flex">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                className="text-muted-foreground"
                aria-label={label}
                data-testid="ticket-copy-link"
                onClick={() => void copy(url)}
              >
                {status === "copied" ? <CheckIcon aria-hidden /> : <LinkIcon aria-hidden />}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">{label}</TooltipContent>
          </Tooltip>
        </span>
      </PopoverAnchor>
      <PopoverContent align="end" className="w-80 space-y-1.5 text-sm" role="status">
        <p className="text-muted-foreground">Couldn't copy automatically. Copy this link:</p>
        <p className="select-all break-all" data-testid="ticket-copy-link-fallback">
          {url}
        </p>
      </PopoverContent>
    </Popover>
  );
}
