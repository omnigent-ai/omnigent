/**
 * A gadget's link (`/gadget/:gadgetId`). It replaces itself with the gadget filling the screen
 * where it opens (`?gadget=`), keeping the sidebar for an open from inside Omnigent and closing it
 * for a link opened fresh.
 */
import { useCallback, useEffect, useState } from "react";
import { Loader2Icon } from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { Link, useLocation, useNavigate, useParams } from "@/lib/routing";
import { withGadgetFullScreen } from "@/lib/gadgetRailTabs";
import { openGadgetFullScreen } from "@/gadgets/host/gadgetSession";
import { GadgetUnavailable } from "./GadgetUnavailable";
import { appPathFromUrl, gadgetOpenKeepsSidebar } from "./gadgetNavigation";
import { useGadget } from "./gadgetQueries";

export function GadgetOpenPage() {
  const { gadgetId = "" } = useParams<{ gadgetId: string }>();
  const location = useLocation();
  const keepSidebar = gadgetOpenKeepsSidebar(location);
  const gadget = useGadget(gadgetId);
  const navigate = useNavigate();
  const go = useCallback(
    (url: string) =>
      navigate(withGadgetFullScreen(appPathFromUrl(url), gadgetId), {
        replace: true,
        // The session's URL is a new entry, so its state carries the decision made here.
        state: { keepSidebar },
      }),
    [navigate, gadgetId, keepSidebar],
  );
  const [openError, setOpenError] = useState<unknown>(undefined);
  const [attempt, setAttempt] = useState(0);

  const data = gadget.data;
  useEffect(() => {
    if (data === undefined) return undefined;
    let cancelled = false;
    setOpenError(undefined);
    const open = (url: string) => {
      if (!cancelled) go(url);
    };
    openGadgetFullScreen(data, open, { keepSidebar }).catch((error: unknown) => {
      if (!cancelled) setOpenError(error);
    });
    return () => {
      cancelled = true;
    };
  }, [data, go, keepSidebar, attempt]);

  const error = gadget.isError ? gadget.error : openError;
  if (error === undefined) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center gap-2 text-ui text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" />
        Opening gadget…
      </div>
    );
  }
  return (
    <PageScroll maxWidthClassName="max-w-4xl" contentClassName="px-4 md:px-8">
      <nav aria-label="Breadcrumb" className="mb-4 text-ui text-muted-foreground">
        <Link to="/gadget" className="hover:text-foreground">
          Gadget
        </Link>
      </nav>
      <GadgetUnavailable
        gadgetId={gadgetId}
        error={error}
        onRetry={() => (gadget.isError ? void gadget.refetch() : setAttempt((n) => n + 1))}
      />
    </PageScroll>
  );
}
