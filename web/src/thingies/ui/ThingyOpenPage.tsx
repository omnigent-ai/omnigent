/**
 * A thingy's link (`/thingy/:thingyId`): opens the thingy filling the screen in the viewer's
 * thingy session, or beside New session when they have none, so leaving full screen shows the
 * session behind it. Opened from inside Omnigent (a pinned row, the Thingy page), the sidebar stays
 * as it was; a link opened fresh closes it.
 *
 * The page replaces itself with where the thingy opens, and that URL names the thingy filling
 * the screen (`?thingy=`), so Back returns to the page before the link and Forward opens the
 * thingy full screen again.
 */
import { useCallback, useEffect, useState } from "react";
import { Loader2Icon } from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { Link, useLocation, useNavigate, useParams } from "@/lib/routing";
import { withThingyFullScreen } from "@/lib/thingyRailTabs";
import { openThingyFullScreen } from "@/thingies/host/thingySession";
import { ThingyUnavailable } from "./ThingyUnavailable";
import { appPathFromUrl, thingyOpenKeepsSidebar } from "./thingyNavigation";
import { useThingy } from "./thingyQueries";

export function ThingyOpenPage() {
  const { thingyId = "" } = useParams<{ thingyId: string }>();
  const location = useLocation();
  const keepSidebar = thingyOpenKeepsSidebar(location);
  const thingy = useThingy(thingyId);
  const navigate = useNavigate();
  const go = useCallback(
    (url: string) =>
      navigate(withThingyFullScreen(appPathFromUrl(url), thingyId), {
        replace: true,
        // The session's URL is a new entry, so its state carries the decision made here.
        state: { keepSidebar },
      }),
    [navigate, thingyId, keepSidebar],
  );
  const [openError, setOpenError] = useState<unknown>(undefined);
  const [attempt, setAttempt] = useState(0);

  const data = thingy.data;
  useEffect(() => {
    if (data === undefined) return undefined;
    let cancelled = false;
    setOpenError(undefined);
    const open = (url: string) => {
      if (!cancelled) go(url);
    };
    openThingyFullScreen(data, open, { keepSidebar }).catch((error: unknown) => {
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
    <PageScroll maxWidthClassName="max-w-4xl" contentClassName="px-4 md:px-8">
      <nav aria-label="Breadcrumb" className="mb-4 text-ui text-muted-foreground">
        <Link to="/thingy" className="hover:text-foreground">
          Thingy
        </Link>
      </nav>
      <ThingyUnavailable
        thingyId={thingyId}
        error={error}
        onRetry={() => (thingy.isError ? void thingy.refetch() : setAttempt((n) => n + 1))}
      />
    </PageScroll>
  );
}
