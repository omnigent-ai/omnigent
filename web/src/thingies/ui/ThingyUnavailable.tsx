// What a viewer sees in a thingy's place when they can't open it: it was deleted, or its owner
// stopped sharing it with them. The server answers both the same way (404), so the text names
// both, and a pinned thingy offers Unpin so the sidebar row can go.
import { PanelsTopLeftIcon, PinOffIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { ThingyErrorNotice } from "./ThingyErrorNotice";
import { isThingyUnavailable, usePinThingy, useThingyShelf } from "./thingyQueries";

export function ThingyUnavailable({
  thingyId,
  error,
  onRetry,
  className,
}: {
  thingyId: string;
  /** Why it didn't open; a failure other than a missing thingy shows with Retry. */
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  const shelf = useThingyShelf();
  const pin = usePinThingy();
  if (!isThingyUnavailable(error)) {
    return (
      <div className={cn("flex flex-col gap-4", className)}>
        <ThingyErrorNotice title="Couldn’t open the thingy" error={error} onRetry={onRetry} />
        <BackLink />
      </div>
    );
  }
  const pinned = shelf.data?.pinned.find((item) => item.id === thingyId);
  return (
    <div
      role="status"
      data-testid="thingy-unavailable"
      className={cn(
        "flex min-h-60 flex-1 flex-col items-center justify-center gap-2 px-6 text-center",
        className,
      )}
    >
      <PanelsTopLeftIcon className="size-8 text-muted-foreground/50" />
      <p className="text-ui font-medium">
        {pinned ? `“${pinned.name}” isn’t available` : "This thingy isn’t available"}
      </p>
      <p className="max-w-sm text-sm text-muted-foreground">
        It was deleted, or you no longer have access to it. Ask its owner to share it with you
        again.
      </p>
      <div className="mt-2 flex flex-wrap items-center justify-center gap-2">
        {pinned && (
          <Button
            variant="outline"
            loading={pin.isPending}
            componentId="thingies.unavailable.unpin"
            onClick={() => pin.mutate({ thingyId, pinned: false })}
          >
            <PinOffIcon />
            Unpin from sidebar
          </Button>
        )}
        <Button asChild variant={pinned ? "ghost" : "outline"}>
          <Link to="/thingy">Back to your thingies</Link>
        </Button>
      </div>
    </div>
  );
}

function BackLink() {
  return (
    <p className="text-ui text-muted-foreground">
      <Link to="/thingy" className="underline underline-offset-3 hover:text-foreground">
        Back to your thingies
      </Link>
    </p>
  );
}
