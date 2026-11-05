// A changed file's header actions in the Changes tab: copy its path and open it on the provider.
// Desktop shows the copy icon; on mobile every action lives in the overflow menu.

import { CopyIcon, EllipsisIcon } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { copyText } from "@/lib/clipboard";
import { IconButton } from "./PullRequestFileSection";

export function PullRequestFileActions({
  path,
  previousPath,
  openLabel,
  openUrl,
  openUnavailableReason,
}: {
  /** The repo-relative current path. */
  path: string;
  /** The old path of a renamed file, or null. */
  previousPath: string | null;
  openLabel: string;
  /** Null when the file can't be opened at the revision it shows. */
  openUrl: string | null;
  openUnavailableReason: string;
}) {
  const mobile = useIsMobileViewport();
  const copy = (value: string, label: string) => {
    void copyText(value).then(
      () => toast.success(`Copied ${label}`),
      () => toast.error(`Couldn’t copy ${label}`),
    );
  };
  return (
    <>
      {!mobile && (
        <IconButton
          label={`Copy path ${path}`}
          onClick={() => copy(path, "path")}
          className="size-5 text-muted-foreground"
        >
          <CopyIcon className="size-3.5" />
        </IconButton>
      )}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`More actions for ${path}`}
            className="shrink-0 text-muted-foreground"
          >
            <EllipsisIcon className="size-3.5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {mobile && (
            <DropdownMenuItem onSelect={() => copy(path, "path")}>Copy path</DropdownMenuItem>
          )}
          {previousPath && (
            <DropdownMenuItem onSelect={() => copy(previousPath, "previous path")}>
              Copy previous path
            </DropdownMenuItem>
          )}
          {openUrl ? (
            <DropdownMenuItem asChild>
              <a href={openUrl} target="_blank" rel="noreferrer">
                {openLabel}
              </a>
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem disabled className="flex-col items-start">
              {openLabel}
              <span className="text-xs text-muted-foreground">{openUnavailableReason}</span>
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}
