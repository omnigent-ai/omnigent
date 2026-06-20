// `/compare?sessions=a,b,c` — the side-by-side multi-session view. Renders the
// selected sessions as live, independently interactive panes. Entered from the
// sidebar's "Open side-by-side" bulk action; the session list lives in the URL
// so the view is shareable and survives a refresh.

import { useCallback, useEffect, useMemo } from "react";
import { useNavigate, useSearchParams } from "@/lib/routing";
import { MultiSessionGrid } from "@/shell/MultiSessionGrid";

/** Parse + de-duplicate the `?sessions=` list, preserving left-to-right order. */
function parseSessionIds(raw: string | null): string[] {
  const seen = new Set<string>();
  const ids: string[] = [];
  for (const part of (raw ?? "").split(",")) {
    const id = part.trim();
    if (id && !seen.has(id)) {
      seen.add(id);
      ids.push(id);
    }
  }
  return ids;
}

export function ComparePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const sessionIds = useMemo(() => parseSessionIds(searchParams.get("sessions")), [searchParams]);

  // Side-by-side needs at least two sessions. With one, fall back to the normal
  // single-session route; with none, the landing page.
  useEffect(() => {
    if (sessionIds.length === 0) navigate("/", { replace: true });
    else if (sessionIds.length === 1) navigate(`/c/${sessionIds[0]}`, { replace: true });
  }, [sessionIds, navigate]);

  const closeSession = useCallback(
    (id: string) => {
      const next = sessionIds.filter((s) => s !== id);
      if (next.length >= 2) {
        setSearchParams({ sessions: next.join(",") }, { replace: true });
      } else if (next.length === 1) {
        navigate(`/c/${next[0]}`, { replace: true });
      } else {
        navigate("/", { replace: true });
      }
    },
    [sessionIds, navigate, setSearchParams],
  );

  // While the redirect effect settles (fewer than two sessions), render nothing.
  if (sessionIds.length < 2) return null;

  return <MultiSessionGrid sessionIds={sessionIds} onCloseSession={closeSession} />;
}
