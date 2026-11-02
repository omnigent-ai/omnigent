import { BotIcon } from "lucide-react";
import { Link, useLocation } from "@/lib/routing";
import { sessionNavigationSearch } from "@/lib/sessionNavigation";
import { readSubagentActivity } from "@/lib/subagentActivity";
import { useOpenSubagentInPanel } from "@/shell/subagentPanelOpener";

export function SubagentActivityMessage({ data }: { data: Record<string, unknown> }) {
  const { search } = useLocation();
  const openInPanel = useOpenSubagentInPanel();
  const activity = readSubagentActivity(data);
  if (!activity) return null;
  const params = new URLSearchParams(sessionNavigationSearch(search));
  params.set("panel", "agents");
  const to = {
    pathname: `/c/${encodeURIComponent(activity.sessionId)}`,
    search: `?${params.toString()}`,
  };
  let label = activity.phase === "delegated" ? "started working" : "finished working";
  if (activity.phase === "returned") {
    if (activity.status === "failed") label = "failed";
    if (["cancelled", "killed", "stopped"].includes(activity.status ?? "")) label = "stopped";
  }
  return (
    <div
      className="not-prose flex min-w-0 items-start gap-2 py-0.5 text-ui text-muted-foreground"
      data-testid="subagent-activity"
      data-phase={activity.phase}
      data-child-session-id={activity.sessionId}
    >
      <BotIcon aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-brand-accent" />
      <span className="min-w-0 break-words">
        <Link
          to={to}
          onClick={(event) => {
            openInPanel(activity.sessionId, event);
          }}
          className="rounded-sm font-medium text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {activity.title}
        </Link>{" "}
        {label}
      </span>
    </div>
  );
}
