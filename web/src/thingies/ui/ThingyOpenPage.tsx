/**
 * A thingy's link (`/thingy/:thingyId`): opens the thingy filling the screen in the viewer's
 * thingy session, or beside New session when they have none, so leaving full screen shows the
 * session behind it. A pinned sidebar row opens it here too and keeps the sidebar open.
 */
import { useEffect, useState } from "react";
import { Loader2Icon } from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { Link, useLocation, useParams } from "@/lib/routing";
import { openThingyFullScreen } from "@/thingies/host/thingySession";
import { ThingyErrorNotice } from "./ThingyErrorNotice";
import { useThingyNavigate } from "./thingyNavigation";
import { useThingy } from "./thingyQueries";

/** Link state a pinned sidebar row passes, so full screen leaves the sidebar open. */
export interface ThingyOpenState {
  keepSidebar?: boolean;
}

export function ThingyOpenPage() {
  const { thingyId = "" } = useParams<{ thingyId: string }>();
  const location = useLocation();
  const keepSidebar = (location.state as ThingyOpenState | null)?.keepSidebar === true;
  const thingy = useThingy(thingyId);
  const go = useThingyNavigate({ replace: true });
  const [openError, setOpenError] = useState<unknown>(undefined);
  const [attempt, setAttempt] = useState(0);

  const data = thingy.data;
  useEffect(() => {
    if (data === undefined) return undefined;
    let cancelled = false;
    setOpenError(undefined);
    const navigate = (url: string) => {
      if (!cancelled) go(url);
    };
    openThingyFullScreen(data, navigate, { keepSidebar }).catch((error: unknown) => {
      if (!cancelled) setOpenError(error);
    });
    return () => {
      cancelled = true;
    };
  }, [data, go, keepSidebar, attempt]);

  const error = thingy.isError ? thingy.error : openError;
  if (error === undefined) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center gap-2 text-ui text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" />
        Opening thingy…
      </div>
    );
  }
  return (
    <PageScroll contentClassName="px-4 md:px-6">
      <nav aria-label="Breadcrumb" className="mb-4 text-ui text-muted-foreground">
        <Link to="/thingy" className="hover:text-foreground">
          Thingy
        </Link>
      </nav>
      <ThingyErrorNotice
        title="Couldn’t open the thingy"
        error={error}
        onRetry={() => (thingy.isError ? void thingy.refetch() : setAttempt((n) => n + 1))}
      />
      <p className="mt-4 text-ui text-muted-foreground">
        <Link to="/thingy" className="underline underline-offset-3 hover:text-foreground">
          Back to your thingies
        </Link>
      </p>
    </PageScroll>
  );
}
